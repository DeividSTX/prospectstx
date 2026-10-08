import datetime as dt
import json
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from analysis import RulesAnalyzer, generate_messages
from providers import PROVIDERS
from campaigns import CampaignService

ROOT = Path(__file__).resolve().parent
STATUSES = ['NEW', 'RESEARCHING', 'QUALIFIED', 'CONTACT_PENDING', 'CONTACTED', 'REPLIED', 'INTERESTED', 'MEETING', 'PROPOSAL', 'WON', 'LOST', 'FOLLOW_UP', 'NOT_INTERESTED']
FIELDS = ['name', 'company', 'instagram', 'website', 'email', 'phone', 'city', 'country', 'niche', 'source', 'description', 'offer', 'audience', 'acquisition', 'notes', 'owner', 'next_action', 'next_action_due']


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


class Store:
    def __init__(self, path=None, config=None):
        self.path = str(path or os.getenv('DATABASE_PATH', ROOT / 'data/prospect.db'))
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.config = config or json.loads(Path(os.getenv('CONFIG_PATH', ROOT / 'config.json')).read_text(encoding='utf-8'))
        if self.config['mode'] not in ('MANUAL', 'ASSISTIDO', 'AUTOMATIZADO') or sum(self.config['weights'].values()) <= 0 or any(v < 0 for v in self.config['weights'].values()):
            raise ValueError('Configuração inválida.')
        self.lock = threading.RLock()
        with self.db() as db:
            db.executescript((ROOT / 'migrations/001_initial.sql').read_text())
            db.execute('INSERT OR IGNORE INTO schema_migrations VALUES (?)', ('001',))
            for migration in sorted((ROOT / 'migrations').glob('*.sql')):
                version = migration.stem.split('_')[0]
                if not db.execute('SELECT 1 FROM schema_migrations WHERE version=?', (version,)).fetchone():
                    db.executescript(migration.read_text(encoding='utf-8'))
                    db.execute('INSERT INTO schema_migrations VALUES (?)', (version,))
        self.campaigns = CampaignService(self)

    @contextmanager
    def db(self):
        with self.lock:
            db = sqlite3.connect(self.path)
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            try:
                with db:
                    yield db
            finally:
                db.close()

    def log(self, db, lid, action, detail=''):
        db.execute('INSERT INTO audit_logs(lead_id,action,detail,created_at) VALUES (?,?,?,?)', (lid, action, detail, now()))

    def normalize(self, raw):
        data = {k: str(raw.get(k) or '').strip()[:3000] for k in FIELDS}
        if not data['company']:
            raise ValueError('Empresa é obrigatória.')
        data['email'] = data['email'].lower()
        if data['email'] and not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', data['email']):
            raise ValueError('E-mail inválido.')
        data['phone'] = re.sub(r'\D', '', data['phone'])
        data['instagram'] = data['instagram'].lower().rstrip('/').split('/')[-1].lstrip('@')
        if data['website']:
            value = data['website'] if '://' in data['website'] else 'https://' + data['website']
            parsed = urlparse(value)
            if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username:
                raise ValueError('Site inválido.')
            data['website'] = value
        evidence = raw.get('evidence', {})
        if not isinstance(evidence, dict):
            raise ValueError('Evidências precisam ser um objeto.')
        for key, item in evidence.items():
            if not isinstance(item, dict) or not isinstance(item.get('verified', False), bool):
                raise ValueError('Evidência inválida.')
            if item.get('verified') and (not item.get('source') or not item.get('note')):
                raise ValueError('Sinal verificado exige fonte e observação.')
        data['evidence'] = evidence
        return data

    def create(self, raw):
        data = self.normalize(raw)
        identities = [('company', ' '.join(data['company'].casefold().split()))]
        identities += [(k, data[k]) for k in ('email', 'instagram') if data[k]]
        if data['website']:
            identities.append(('domain', urlparse(data['website']).hostname.lower().removeprefix('www.')))
        with self.db() as db:
            for kind, value in identities:
                existing = db.execute('SELECT lead_id FROM identities WHERE kind=? AND value=?', (kind, value)).fetchone()
                if existing:
                    self.log(db, existing[0], 'DUPLICATE_SKIPPED', kind)
                    return {'id': existing[0], 'duplicate': True}
            stamp = now()
            lid = db.execute('INSERT INTO leads(company,data,created_at,updated_at) VALUES (?,?,?,?)', (data['company'], json.dumps(data), stamp, stamp)).lastrowid
            db.executemany('INSERT INTO identities VALUES (?,?,?)', [(k, v, lid) for k, v in identities])
            self.log(db, lid, 'LEAD_CREATED', data['source'] or 'manual')
        self.analyze(lid)
        return {'id': lid, 'duplicate': False}

    def get(self, db, lid):
        row = db.execute('SELECT * FROM leads WHERE id=?', (lid,)).fetchone()
        if not row:
            raise ValueError('Lead não encontrado.')
        return dict(row)

    def analyze(self, lid):
        with self.db() as db:
            row = self.get(db, lid)
            data = json.loads(row['data'])
            result = RulesAnalyzer().analyze(data, self.config)
            self.campaigns.invalidate(db, lid, 'Pesquisa refeita: autorização anterior invalidada.')
            if row['do_not_contact']:
                result['qualified'] = False
            db.execute("UPDATE messages SET state='SUPERSEDED' WHERE lead_id=? AND state IN ('PENDING','APPROVED')", (lid,))
            db.execute("UPDATE followups SET state='CANCELLED' WHERE lead_id=? AND state IN ('PENDING','APPROVED')", (lid,))
            status = ('QUALIFIED' if result['qualified'] else 'RESEARCHING') if row['status'] in ('NEW', 'RESEARCHING', 'QUALIFIED', 'CONTACT_PENDING') else row['status']
            db.execute('UPDATE leads SET analysis=?,score=?,status=?,updated_at=? WHERE id=?', (json.dumps(result), result['score'], status, now(), lid))
            if self.config['mode'] != 'MANUAL':
                for variant, body in generate_messages(data, result, self.config['agency_name']):
                    db.execute('INSERT INTO messages(lead_id,variant,body,created_at) VALUES (?,?,?,?)', (lid, variant, body, now()))
            self.log(db, lid, 'ANALYZED', f"Score {result['score']}; {result['classification']}; engine=rules")
        return result

    def update(self, lid, raw):
        # Identidades permanecem imutáveis nesta versão para evitar merges silenciosos.
        with self.db() as db:
            row = self.get(db, lid)
            data = json.loads(row['data'])
            for key in ('offer', 'audience', 'acquisition', 'description', 'notes', 'owner', 'next_action', 'evidence'):
                if key in raw:
                    data[key] = raw[key]
            data = self.normalize(data)
            db.execute('UPDATE leads SET data=?,updated_at=? WHERE id=?', (json.dumps(data), now(), lid))
            self.log(db, lid, 'RESEARCH_UPDATED')
        return self.analyze(lid)

    def schedule_action(self, lid, payload):
        values = {}
        for key in ('owner', 'next_action', 'next_action_due'):
            value = payload.get(key, '')
            if not isinstance(value, str) or len(value) > 3000:
                raise ValueError('Dados da agenda inválidos.')
            values[key] = value.strip()
        due = values['next_action_due']
        if due:
            try:
                if dt.date.fromisoformat(due).isoformat() != due:
                    raise ValueError()
            except ValueError:
                raise ValueError('Prazo inválido. Use uma data válida.')
            if not values['next_action']:
                raise ValueError('Informe a próxima ação antes de definir um prazo.')
        with self.db() as db:
            row = self.get(db, lid)
            data = json.loads(row['data'])
            data.update(values)
            db.execute('UPDATE leads SET data=?,updated_at=? WHERE id=?', (json.dumps(data), now(), lid))
            self.log(db, lid, 'ACTION_SCHEDULED', values['next_action'])
        return values

    def save_meeting(self, lid, payload):
        state = payload.get('state')
        if state not in ('SCHEDULED', 'HELD', 'NO_SHOW', 'CANCELLED'):
            raise ValueError('Situação da reunião inválida.')
        data = {'state': state}
        for key in ('date', 'participant', 'need', 'notes'):
            value = payload.get(key, '')
            if not isinstance(value, str) or len(value) > 3000:
                raise ValueError('Dados da reunião inválidos.')
            data[key] = value.strip()
        try:
            meeting_date = dt.date.fromisoformat(data['date'])
            if meeting_date.isoformat() != data['date']:
                raise ValueError()
        except ValueError:
            raise ValueError('Informe uma data válida para a reunião.')
        if state == 'HELD' and meeting_date > dt.datetime.now(dt.timezone(dt.timedelta(hours=-3))).date():
            raise ValueError('Uma reunião futura não pode ser marcada como realizada.')
        for key in ('decision_maker', 'agency_fit', 'compatible_need'):
            value = payload.get(key, False)
            if not isinstance(value, bool):
                raise ValueError('Critérios da reunião inválidos.')
            data[key] = value
        data['qualified'] = state == 'HELD' and all(data[k] for k in ('decision_maker', 'agency_fit', 'compatible_need'))
        if data['qualified'] and (not data['participant'] or not data['need']):
            raise ValueError('Identifique o decisor e descreva a necessidade compatível.')
        with self.db() as db:
            self.get(db, lid)
            db.execute('INSERT INTO meeting_outcomes(lead_id,data,updated_at) VALUES (?,?,?) ON CONFLICT(lead_id) DO UPDATE SET data=excluded.data,updated_at=excluded.updated_at', (lid, json.dumps(data), now()))
            self.log(db, lid, 'MEETING_RECORDED', json.dumps(data))
        return data

    def import_csv(self, content):
        report = {'created': 0, 'duplicates': 0, 'errors': []}
        for index, row in enumerate(PROVIDERS['csv'].collect(content), 2):
            try:
                result = self.create(row)
                report['duplicates' if result['duplicate'] else 'created'] += 1
            except ValueError as error:
                report['errors'].append({'line': index, 'error': str(error)})
        with self.db() as db:
            self.log(db, None, 'CSV_IMPORTED', json.dumps(report))
        return report

    def act(self, lid, action, payload):
        with self.db() as db:
            row = self.get(db, lid)
            if action == 'delete':
                contact = db.execute('SELECT recipient_id FROM instagram_contacts WHERE lead_id=?', (lid,)).fetchone()
                if contact:
                    db.execute('DELETE FROM inbox_events WHERE recipient_id=?', (contact[0],))
                    db.execute('UPDATE instagram_contacts SET suppressed=1 WHERE recipient_id=?', (contact[0],))
                self.campaigns.invalidate(db, lid, 'Lead excluído.')
                db.execute('DELETE FROM leads WHERE id=?', (lid,))
                self.log(db, None, 'LEAD_DELETED', 'Dados e histórico removidos.')
                return
            if action == 'do_not_contact':
                self.campaigns.invalidate(db, lid, 'DO_NOT_CONTACT: contato bloqueado.')
                db.execute('UPDATE instagram_contacts SET suppressed=1 WHERE lead_id=?', (lid,))
                db.execute("UPDATE leads SET do_not_contact=1,status='NOT_INTERESTED',updated_at=? WHERE id=?", (now(), lid))
                db.execute("UPDATE messages SET state='CANCELLED' WHERE lead_id=? AND state IN ('PENDING','APPROVED')", (lid,))
                db.execute("UPDATE followups SET state='CANCELLED' WHERE lead_id=? AND state IN ('PENDING','APPROVED')", (lid,))
            elif action in ('approve', 'reject', 'edit', 'contact'):
                if row['do_not_contact']:
                    raise ValueError('DO_NOT_CONTACT impede novas abordagens.')
                msg = db.execute('SELECT * FROM messages WHERE id=? AND lead_id=?', (payload.get('message_id'), lid)).fetchone()
                if not msg:
                    raise ValueError('Mensagem não encontrada.')
                if action in ('approve', 'reject', 'edit'):
                    if action in ('reject', 'edit') or payload.get('body', msg['body']) != msg['body']:
                        self.campaigns.invalidate(db, lid, 'Mensagem editada ou rejeitada.')
                    if msg['state'] not in ('PENDING', 'APPROVED'):
                        raise ValueError('Mensagem não está disponível para revisão.')
                    body = str(payload.get('body', msg['body'])).strip()
                    if not body or len(body) > 4000:
                        raise ValueError('Mensagem deve ter entre 1 e 4000 caracteres.')
                    state = {'approve': 'APPROVED', 'reject': 'REJECTED', 'edit': 'PENDING'}[action]
                    db.execute('UPDATE messages SET body=?,state=?,approved_at=? WHERE id=?', (body, state, now() if action == 'approve' else None, msg['id']))
                    if action in ('edit', 'reject'):
                        db.execute('UPDATE followups SET state=? WHERE message_id=?', ('PENDING' if action == 'edit' else 'REJECTED', msg['id']))
                    if action == 'approve':
                        db.execute("UPDATE leads SET status='CONTACT_PENDING' WHERE id=?", (lid,))
                        db.execute("UPDATE followups SET state='APPROVED' WHERE message_id=?", (msg['id'],))
                else:
                    if db.execute("SELECT 1 FROM campaign_items WHERE message_id=? AND state IN ('READY','QUEUED','SENDING')", (msg['id'],)).fetchone():
                        raise ValueError('Mensagem reservada por campanha: pause e cancele antes de registrar manualmente.')
                    if msg['state'] != 'APPROVED':
                        raise ValueError('Aprovação da mensagem atual é obrigatória.')
                    if 'body' in payload and payload['body'] != msg['body']:
                        raise ValueError('Texto alterado exige nova aprovação.')
                    followup = db.execute('SELECT * FROM followups WHERE message_id=?', (msg['id'],)).fetchone()
                    if followup and (followup['state'] != 'APPROVED' or followup['due_at'] > now()):
                        raise ValueError('Follow-up ainda não está vencido e aprovado.')
                    count = db.execute("SELECT COUNT(*) FROM interactions WHERE kind='CONTACT' AND substr(created_at,1,10)=?", (now()[:10],)).fetchone()[0]
                    if count >= self.config['max_daily_actions']:
                        raise ValueError('Limite diário de contatos atingido.')
                    db.execute("UPDATE messages SET state='USED' WHERE id=?", (msg['id'],))
                    db.execute("UPDATE messages SET state='SUPERSEDED' WHERE lead_id=? AND id<>? AND state IN ('PENDING','APPROVED')", (lid, msg['id']))
                    db.execute("UPDATE followups SET state='DONE' WHERE message_id=?", (msg['id'],))
                    db.execute("INSERT INTO interactions(lead_id,message_id,kind,note,created_at) VALUES (?,?,'CONTACT',?,?)", (lid, msg['id'], str(payload.get('note', 'Contato manual registrado.'))[:3000], now()))
                    db.execute("UPDATE leads SET status='CONTACTED',updated_at=? WHERE id=?", (now(), lid))
                    if msg['variant'] != 'FOLLOW_UP':
                        db.execute("UPDATE followups SET state='CANCELLED' WHERE lead_id=? AND state IN ('PENDING','APPROVED')", (lid,))
                        for day in self.config['followup_days'][:2]:
                            due = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=day)).isoformat()
                            mid = db.execute("INSERT INTO messages(lead_id,variant,body,created_at) VALUES (?,'FOLLOW_UP',?,?)", (lid, f"Oi, equipe da {row['company']}. Sobre a ideia que compartilhei: faz sentido retomar? Se não for uma prioridade, encerro por aqui.", now())).lastrowid
                            db.execute('INSERT INTO followups(lead_id,due_at,message_id) VALUES (?,?,?)', (lid, due, mid))
            elif action == 'status':
                status = payload.get('status')
                if status not in STATUSES:
                    raise ValueError('Status inválido.')
                if row['do_not_contact'] and status != 'NOT_INTERESTED':
                    raise ValueError('Lead bloqueado para contato.')
                if status == 'CONTACTED':
                    raise ValueError('Use registrar contato após aprovação.')
                db.execute('UPDATE leads SET status=?,updated_at=? WHERE id=?', (status, now(), lid))
                db.execute("INSERT INTO interactions(lead_id,kind,note,created_at) VALUES (?,'STATUS',?,?)", (lid, status + ': ' + str(payload.get('note', ''))[:3000], now()))
                if status in ('REPLIED', 'INTERESTED', 'MEETING', 'PROPOSAL', 'WON', 'LOST', 'NOT_INTERESTED'):
                    self.campaigns.invalidate(db, lid, 'Estágio atualizado: revisar antes de novo envio.')
                    db.execute("UPDATE messages SET state='CANCELLED' WHERE id IN (SELECT message_id FROM followups WHERE lead_id=?) AND state IN ('PENDING','APPROVED')", (lid,))
                    db.execute("UPDATE followups SET state='CANCELLED' WHERE lead_id=? AND state IN ('PENDING','APPROVED')", (lid,))
            else:
                raise ValueError('Ação desconhecida.')
            self.log(db, lid, action.upper())

    def snapshot(self):
        with self.db() as db:
            leads = []
            for row in db.execute('SELECT * FROM leads ORDER BY score DESC,id DESC'):
                item = dict(row)
                item['data'] = json.loads(item['data'])
                item['analysis'] = json.loads(item['analysis']) if item['analysis'] else None
                for table in ('messages', 'interactions', 'followups', 'audit_logs'):
                    item[table] = [dict(x) for x in db.execute(f'SELECT * FROM {table} WHERE lead_id=? ORDER BY id DESC', (item['id'],))]
                meeting = db.execute('SELECT data FROM meeting_outcomes WHERE lead_id=?', (item['id'],)).fetchone()
                item['meeting'] = json.loads(meeting[0]) if meeting else None
                leads.append(item)
            logs = [dict(x) for x in db.execute('SELECT * FROM audit_logs ORDER BY id DESC LIMIT 100')]
            contact_ids = {r[0] for r in db.execute("SELECT DISTINCT lead_id FROM interactions WHERE kind='CONTACT'")}
            reply_ids = {r[0] for r in db.execute("SELECT DISTINCT lead_id FROM interactions WHERE kind='STATUS' AND note LIKE 'REPLIED:%'")}
            won = sum(l['status'] == 'WON' for l in leads)
            metrics = {'leads': len(leads), 'analyzed': sum(bool(l['analysis']) for l in leads), 'qualified': sum(bool(l['analysis'] and l['analysis']['qualified']) for l in leads), 'contacted': len(contact_ids), 'replies': len(reply_ids), 'meetings': sum(l['status'] == 'MEETING' for l in leads), 'proposals': sum(l['status'] == 'PROPOSAL' for l in leads), 'won': won, 'response_rate': round(len(reply_ids & contact_ids) / len(contact_ids) * 100, 1) if contact_ids else 0, 'conversion_rate': round(won / len(leads) * 100, 1) if leads else 0}
            today = now()[:10]
            daily = {'date_utc': today, 'found': sum(l['created_at'][:10] == today for l in leads), 'analyzed': db.execute("SELECT COUNT(DISTINCT lead_id) FROM audit_logs WHERE action='ANALYZED' AND substr(created_at,1,10)=?", (today,)).fetchone()[0], 'pending_messages': sum(m['state'] == 'PENDING' for l in leads for m in l['messages']), 'due_followups': sum(f['state'] in ('PENDING', 'APPROVED') and f['due_at'] <= now() for l in leads for f in l['followups']), 'errors': [x for x in logs if x['action'] == 'ERROR' and x['created_at'][:10] == today], 'top_leads': [l['company'] for l in leads if l['analysis'] and l['analysis']['qualified']][:10]}
            result = {'leads': leads, 'metrics': metrics, 'daily': daily, 'logs': logs, 'statuses': STATUSES, 'config': self.config, 'integrations': {'analysis': 'Análise de oportunidades por regras; revisão OpenAI opcional', 'sources': 'CSV, manual e mensagens recebidas do Instagram via webhook', 'sending': 'Adapter oficial Instagram; depende de configuração e autorização de campanha'}}
        result.update(self.campaigns.snapshot())
        from prospecting import snapshot as prospect_snapshot
        result['prospecting'] = prospect_snapshot(self)
        return result

    def configure(self, payload):
        proposed = dict(self.config)
        for key in ('agency_name', 'min_lead_score', 'max_daily_actions', 'target_niches', 'excluded_niches', 'mode'):
            if key in payload:
                proposed[key] = payload[key]
        if not isinstance(proposed['agency_name'], str) or not 1 <= len(proposed['agency_name'].strip()) <= 80:
            raise ValueError('Nome da agência inválido.')
        if proposed['mode'] not in ('MANUAL', 'ASSISTIDO', 'AUTOMATIZADO'):
            raise ValueError('Modo inválido.')
        for key, low, high in [('min_lead_score', 0, 100), ('max_daily_actions', 1, 100)]:
            if type(proposed[key]) is not int or not low <= proposed[key] <= high:
                raise ValueError('Limite inválido.')
        for key in ('target_niches', 'excluded_niches'):
            if not isinstance(proposed[key], list) or not all(isinstance(x, str) for x in proposed[key]):
                raise ValueError('Lista de nichos inválida.')
        path = Path(os.getenv('CONFIG_PATH', ROOT / 'config.json'))
        with self.lock:
            temporary = path.with_suffix('.json.tmp')
            temporary.write_text(json.dumps(proposed, ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(path)
            self.config = proposed
            with self.db() as db:
                db.execute("UPDATE campaigns SET state='PAUSED',note='Configuração alterada: autorize novamente.' WHERE state='ACTIVE'")
                self.log(db, None, 'SETTINGS_UPDATED', 'Campanhas ativas pausadas para revisão.')
        return proposed


def handler(store, auth=None):
    from auth import Auth
    auth = auth or Auth(store)
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(30)

        def log_message(self, *args):
            pass

        def respond(self, code, data, content_type='application/json; charset=utf-8', headers=None):
            body = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def valid_host(self):
            return len(self.headers.get_all('Host', [])) == 1 and self.headers.get('Host') in auth.hosts(self.server.server_port)

        def do_GET(self):
            if urlparse(self.path).path == '/webhooks/instagram':
                query = parse_qs(urlparse(self.path).query)
                env = store.campaigns.adapter.env
                import hmac
                token = env.get('META_VERIFY_TOKEN', '')
                if token and query.get('hub.mode') == ['subscribe'] and hmac.compare_digest(query.get('hub.verify_token', [''])[0], token):
                    return self.respond(200, query.get('hub.challenge', [''])[0].encode(), 'text/plain')
                return self.respond(403, {'error': 'Verificação inválida.'})
            if not self.valid_host():
                return self.respond(403, {'error': 'Host não permitido.'})
            path = urlparse(self.path).path
            if path == '/healthz':
                return self.respond(200, {'ok': True})
            files = {'/': ('index.html', 'text/html'), '/app.js': ('app.js', 'text/javascript'), '/style.css': ('style.css', 'text/css'), '/login': ('login.html', 'text/html'), '/login.html': ('login.html', 'text/html'), '/login.js': ('login.js', 'text/javascript')}
            if path in ('/login', '/login.html', '/login.js', '/style.css'):
                name, mime = files[path]
                return self.respond(200, (ROOT / 'static' / name).read_bytes(), mime + '; charset=utf-8')
            if not auth.authenticated(self.headers.get('Cookie')):
                return self.respond(302 if path == '/' else 401, {'error': 'Faça login para continuar.'}, headers={'Location': '/login'} if path == '/' else None)
            if path == '/api/state':
                return self.respond(200, store.snapshot())
            if path in files:
                name, mime = files[path]
                return self.respond(200, (ROOT / 'static' / name).read_bytes(), mime + '; charset=utf-8')
            self.respond(404, {'error': 'Não encontrado.'})

        def do_POST(self):
            if len(self.headers.get_all('Content-Length', [])) != 1 or self.headers.get('Transfer-Encoding'):
                return self.respond(400, {'error': 'Comprimento da requisição inválido.'})
            if urlparse(self.path).path == '/webhooks/instagram':
                try:
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= 1_000_000:
                        raise ValueError('Evento excede limite.')
                    result = store.campaigns.receive(self.rfile.read(size), self.headers.get('X-Hub-Signature-256', ''))
                    with store.db() as db:
                        store.log(db, None, 'WEBHOOK_ACCEPTED', f"Mensagens registradas: {result['processed']}")
                    return self.respond(200, result)
                except (ValueError, TypeError, KeyError, OverflowError) as error:
                    with store.db() as db:
                        reason = 'Assinatura não autenticada.' if isinstance(error, ValueError) and str(error) == 'Assinatura inválida.' else 'Formato de evento inválido.'
                        store.log(db, None, 'WEBHOOK_REJECTED', reason)
                    return self.respond(403, {'error': 'Evento inválido ou não autenticado.'})
                except Exception:
                    return self.respond(500, {'error': 'Falha ao processar evento; tente novamente.'})
            origin = self.headers.get('Origin')
            if not self.valid_host() or (origin and origin not in auth.origins(self.server.server_port)) or self.headers.get('X-Requested-With') != 'ProspectLocal':
                return self.respond(403, {'error': 'Origem não permitida.'})
            path = urlparse(self.path).path
            if path != '/api/auth/login' and not auth.authenticated(self.headers.get('Cookie')):
                return self.respond(401, {'error': 'Faça login para continuar.'})
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if size < 0 or size > 2_000_000:
                    raise ValueError('Payload excede 2 MB.')
                if path.startswith('/api/auth/') and size > 4096:
                    raise ValueError('Formulário excede limite.')
                payload = json.loads(self.rfile.read(size))
                if not isinstance(payload, dict):
                    raise ValueError('Objeto JSON obrigatório.')
                path = urlparse(self.path).path
                if path == '/api/auth/login':
                    token, status = auth.login(payload.get('user'), payload.get('password'), self.client_address[0])
                    if status != 200:
                        return self.respond(status, {'error': 'Muitas tentativas. Aguarde 15 minutos.' if status == 429 else 'Usuário ou senha inválidos.'})
                    return self.respond(200, {'ok': True}, headers={'Set-Cookie': auth.cookie(token)})
                elif path == '/api/auth/logout':
                    auth.logout(self.headers.get('Cookie'))
                    return self.respond(200, {'ok': True}, headers={'Set-Cookie': auth.cookie()})
                elif path == '/api/leads':
                    result = store.create(payload)
                elif path == '/api/import':
                    result = store.import_csv(payload['csv'])
                elif path == '/api/settings':
                    result = store.configure(payload)
                elif path == '/api/campaigns':
                    result = store.campaigns.create(payload)
                elif path == '/api/inbox/link':
                    result = store.campaigns.link(str(payload['recipient_id']), int(payload['lead_id']))
                elif path == '/api/inbox/reply':
                    result = store.campaigns.prepare_reply(payload)
                elif path == '/api/prospecting/discover':
                    from prospecting import discover
                    result = discover(store, payload)
                elif path == '/api/prospecting/update':
                    from prospecting import update
                    result = update(store, payload)
                elif path == '/api/prospecting/settings':
                    from prospecting import configure_schedule
                    result = configure_schedule(store, payload)
                elif path == '/api/automation/settings':
                    from automation import configure
                    result = configure(store, payload)
                elif path == '/api/inbox/automation':
                    from automation import pause_contact
                    result = pause_contact(store, str(payload['recipient_id']), payload['paused'])
                elif re.fullmatch(r'/api/campaigns/\d+/reconcile', path):
                    result = store.campaigns.reconcile(int(path.split('/')[3]), payload)
                elif re.fullmatch(r'/api/campaigns/\d+/(authorize|pause|cancel)', path):
                    parts = path.split('/')
                    result = store.campaigns.change(int(parts[3]), parts[4])
                elif re.fullmatch(r'/api/leads/\d+/review', path):
                    result = store.campaigns.review(int(path.split('/')[3]), str(payload['body']))
                else:
                    match = re.fullmatch(r'/api/leads/(\d+)/(meeting|schedule|analyze|research|approve|reject|edit|contact|status|do_not_contact|delete)', path)
                    if not match:
                        return self.respond(404, {'error': 'Rota desconhecida.'})
                    lid, action = int(match[1]), match[2]
                    result = store.save_meeting(lid, payload) if action == 'meeting' else store.schedule_action(lid, payload) if action == 'schedule' else store.analyze(lid) if action == 'analyze' else store.update(lid, payload) if action == 'research' else store.act(lid, action, payload)
                self.respond(200, {'ok': True, 'result': result})
            except (ValueError, KeyError, TypeError) as error:
                self.respond(400, {'error': str(error)})
            except Exception:
                with store.db() as db:
                    store.log(db, None, 'ERROR', 'Falha interna; detalhes não expostos.')
                self.respond(500, {'error': 'Falha interna.'})
    return Handler


if __name__ == '__main__':
    env = ROOT / '.env'
    if env.exists():
        for line in env.read_text(encoding='utf-8').splitlines():
            if line.strip() and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ.setdefault(key.strip(), value.strip())
    host = os.getenv('HOST', '127.0.0.1')
    from auth import Auth
    if host not in ('localhost', '127.0.0.1') and not os.getenv('PUBLIC_URL'):
        raise SystemExit('Configure PUBLIC_URL HTTPS e login antes de abrir acesso externo.')
    store = Store()
    auth = Auth(store)
    worker = threading.Thread(target=store.campaigns.run, daemon=True, name='campaign-worker')
    worker.start()
    from prospecting import run as run_prospecting
    threading.Thread(target=run_prospecting, args=(store,), daemon=True, name='prospecting-worker').start()
    server = ThreadingHTTPServer((host, int(os.getenv('PORT', '8765'))), handler(store, auth))
    print(f'Prospect: http://{host}:{server.server_port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        store.campaigns.stop.set()
        server.server_close()
