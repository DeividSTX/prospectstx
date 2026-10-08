import datetime as dt
import hashlib
import json
import re
import threading
from contextlib import nullcontext

from integrations import DeliveryError, InstagramAdapter, OpenAIReviewer
from analysis import RulesAnalyzer


def stamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def fingerprint(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def review_message(body, lead, reply=False):
    issues = []
    if not body.strip() or len(body.encode('utf-8')) > 1000:
        issues.append('Mensagem deve ter até 1000 bytes UTF-8.')
    if re.search(r'garantid|última chance|ultima chance|só hoje|so hoje|dobrar.*vend|100%|incrível|incrivel', body, re.I):
        issues.append('Revise promessa, elogio genérico ou urgência.')
    evidence = [e for e in lead.get('evidence', {}).values() if e.get('verified') is True and e.get('source') and e.get('note')]
    if reply:
        return {'passed': not issues, 'issues': issues, 'engine': 'Regras locais',
                'note': 'Resposta a conversa recebida; confira contexto e fatos antes de autorizar.'}
    if not evidence:
        issues.append('Faltam evidências verificadas.')
    elif not any(e['note'].casefold() in body.casefold() for e in evidence):
        issues.append('Inclua uma observação registrada para personalizar a mensagem.')
    if not re.search(r'pode|talvez|hipótese|hipotese|avaliar|possível|possivel', body, re.I):
        issues.append('Apresente a oportunidade como hipótese, não como certeza.')
    return {'passed': not issues, 'issues': issues, 'engine': 'Regras locais',
            'note': 'Checklist operacional; não substitui a confirmação humana dos fatos.'}


class CampaignService:
    def __init__(self, store, adapter=None, reviewer=None):
        self.store = store
        self.adapter = adapter or InstagramAdapter()
        self.reviewer = reviewer or OpenAIReviewer()
        self.stop = threading.Event()

    def eligible(self, db, lid, reply=False):
        lead = self.store.get(db, lid)
        if lead['do_not_contact']:
            return None, 'Lead pediu para não receber contato.'
        contact = db.execute('SELECT * FROM instagram_contacts WHERE lead_id=?', (lid,)).fetchone()
        if not contact:
            return None, 'Sem conversa recebida e vinculada pela integração.'
        if contact['suppressed']:
            return None, 'Contato bloqueado.'
        if not reply and lead['status'] in ('NOT_INTERESTED', 'LOST', 'WON'):
            return None, 'Lead em etapa de encerramento; não elegível para campanha de prospecção.'
        if not reply and not RulesAnalyzer().analyze(json.loads(lead['data']), self.store.config)['qualified']:
            return None, 'Lead não atende aos critérios atuais de qualificação.'
        inbound = dt.datetime.fromisoformat(contact['last_inbound_at'])
        age = dt.datetime.now(dt.timezone.utc) - inbound
        if age.total_seconds() < 0 or age >= dt.timedelta(hours=24):
            return None, 'Janela operacional de 24 horas encerrada.'
        return dict(contact), ''

    def review(self, lid, body, use_ai=True, reply=False):
        with self.store.db() as db:
            lead = json.loads(self.store.get(db, lid)['data'])
            evidence = lead.get('evidence', {})
            if reply:
                evidence = {'received_messages': [dict(row) for row in db.execute('SELECT body,occurred_at FROM inbox_events WHERE recipient_id IN (SELECT recipient_id FROM instagram_contacts WHERE lead_id=?) ORDER BY occurred_at DESC LIMIT 5', (lid,))]}
                from automation import settings
                evidence['agency_profile'] = settings(self.store)
        result = review_message(body, lead, reply=reply)
        if use_ai:
            ai = self.reviewer.review(body, evidence)
            if ai:
                result['ai'] = ai
                result['engine'] = 'OpenAI + regras locais'
                result['passed'] = result['passed'] and ai['safe'] and not ai['issues']
                result['issues'] += ai['issues']
        return result

    def create(self, payload, db=None):
        name = str(payload.get('name', '')).strip()
        selections = payload.get('selections', [])
        if not name or len(name) > 120 or not isinstance(selections, list) or not 1 <= len(selections) <= 100:
            raise ValueError('Informe nome e de 1 a 100 mensagens, uma por lead.')
        limit = int(payload.get('daily_limit', 10))
        interval = int(payload.get('interval_seconds', 60))
        if not 1 <= limit <= self.store.config['max_daily_actions'] or not 30 <= interval <= 86400:
            raise ValueError('Limite diário inválido ou intervalo inferior a 30 segundos.')
        with (self.store.db() if db is None else nullcontext(db)) as db:
            items, seen = [], set()
            for selected in selections:
                lid, mid = int(selected['lead_id']), int(selected['message_id'])
                if lid in seen:
                    raise ValueError('Selecione apenas uma mensagem por lead.')
                seen.add(lid)
                msg = db.execute('SELECT * FROM messages WHERE id=? AND lead_id=?', (mid, lid)).fetchone()
                contact, reason = self.eligible(db, lid, reply=bool(msg and msg['variant'] == 'REPLY'))
                if reason:
                    raise ValueError(reason)
                msg = db.execute('SELECT * FROM messages WHERE id=? AND lead_id=?', (mid, lid)).fetchone()
                if not msg or msg['state'] not in ('PENDING', 'APPROVED') or msg['variant'] == 'FOLLOW_UP':
                    raise ValueError('Selecione uma abordagem inicial disponível.')
                busy = db.execute("SELECT 1 FROM campaign_items WHERE lead_id=? AND state IN ('READY','QUEUED','SENDING') AND campaign_id IN (SELECT id FROM campaigns WHERE state IN ('DRAFT','ACTIVE','PAUSED'))", (lid,)).fetchone()
                if busy:
                    raise ValueError('Lead já está em outra campanha pendente.')
                if db.execute("SELECT 1 FROM campaign_items WHERE lead_id=? AND state='UNKNOWN'", (lid,)).fetchone():
                    raise ValueError('Envio anterior tem resultado incerto. Confira a conversa e reconcilie antes de criar novo envio.')
                data = json.loads(self.store.get(db, lid)['data'])
                review = review_message(msg['body'], data, reply=msg['variant'] == 'REPLY')
                items.append((lid, mid, contact['recipient_id'], msg['body'], fingerprint(data), json.dumps(review)))
            cid = db.execute('INSERT INTO campaigns(name,daily_limit,interval_seconds,created_at) VALUES (?,?,?,?)', (name, limit, interval, stamp())).lastrowid
            db.executemany('INSERT INTO campaign_items(campaign_id,lead_id,message_id,recipient_id,body,evidence_hash,review) VALUES (?,?,?,?,?,?,?)', [(cid, *i) for i in items])
            self.store.log(db, None, 'CAMPAIGN_CREATED', f'Campanha {cid}: {len(items)} destinatários.')
        return {'id': cid}

    def change(self, cid, action):
        with self.store.db() as db:
            campaign = db.execute('SELECT * FROM campaigns WHERE id=?', (cid,)).fetchone()
            if not campaign:
                raise ValueError('Campanha não encontrada.')
            if action == 'pause':
                if campaign['state'] != 'ACTIVE':
                    raise ValueError('Campanha não está ativa.')
                db.execute("UPDATE campaigns SET state='PAUSED',note='Pausada pelo operador.' WHERE id=?", (cid,))
            elif action == 'cancel':
                db.execute("UPDATE campaigns SET state='CANCELLED',note='Cancelada pelo operador.' WHERE id=?", (cid,))
                db.execute("UPDATE campaign_items SET state='CANCELLED' WHERE campaign_id=? AND state IN ('READY','QUEUED')", (cid,))
            elif action == 'authorize':
                if campaign['automation_hash']:
                    from automation import settings
                    cfg = settings(self.store)
                    if not cfg['enabled'] or fingerprint(cfg) != campaign['automation_hash']:
                        raise ValueError('Atendimento automático desativado ou roteiro alterado.')
                if campaign['state'] not in ('DRAFT', 'PAUSED'):
                    raise ValueError('Campanha não está disponível para autorização.')
                if not self.adapter.status()['ready']:
                    raise ValueError('Configure Instagram e habilite INSTAGRAM_SEND_ENABLED no .env antes de autorizar.')
                items = db.execute("SELECT * FROM campaign_items WHERE campaign_id=? AND state IN ('READY','QUEUED')", (cid,)).fetchall()
                if db.execute("SELECT 1 FROM campaign_items WHERE campaign_id=? AND state IN ('BLOCKED','FAILED','UNKNOWN','SENDING')", (cid,)).fetchone():
                    raise ValueError('Há itens bloqueados ou incertos. Confira as conversas, cancele e prepare uma nova campanha.')
                if not items:
                    raise ValueError('Não há itens disponíveis. Itens com falha exigem conferência e nova campanha.')
                for item in items:
                    msg = db.execute('SELECT * FROM messages WHERE id=?', (item['message_id'],)).fetchone()
                    _, reason = self.eligible(db, item['lead_id'], reply=bool(msg and msg['variant'] == 'REPLY'))
                    if reason:
                        raise ValueError(reason)
                    lead = json.loads(self.store.get(db, item['lead_id'])['data'])
                    msg = db.execute('SELECT * FROM messages WHERE id=?', (item['message_id'],)).fetchone()
                    if msg['state'] not in ('PENDING', 'APPROVED') or msg['body'] != item['body'] or fingerprint(lead) != item['evidence_hash']:
                        raise ValueError('Mensagem ou pesquisa alterada: cancele e recrie a campanha.')
                    review = self.review(item['lead_id'], item['body'], reply=msg['variant'] == 'REPLY')
                    if not review['passed']:
                        raise ValueError('Revisão bloqueou envio: ' + '; '.join(review['issues']))
                    db.execute('UPDATE campaign_items SET review=? WHERE id=?', (json.dumps(review), item['id']))
                    db.execute("UPDATE messages SET state='APPROVED',approved_at=? WHERE id=?", (stamp(), item['message_id']))
                expiration = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=24)).isoformat()
                db.execute("UPDATE campaigns SET state='ACTIVE',authorized_at=?,expires_at=?,note='' WHERE id=?", (stamp(), expiration, cid))
                db.execute("UPDATE campaign_items SET state='QUEUED' WHERE campaign_id=? AND state='READY'", (cid,))
            else:
                raise ValueError('Ação inválida.')
            self.store.log(db, None, 'CAMPAIGN_' + action.upper(), f'Campanha {cid}')

    def invalidate(self, db, lid, reason):
        campaigns = [r[0] for r in db.execute("SELECT DISTINCT campaign_id FROM campaign_items WHERE lead_id=? AND state IN ('READY','QUEUED')", (lid,))]
        db.execute("UPDATE campaign_items SET state='BLOCKED',error=? WHERE lead_id=? AND state IN ('READY','QUEUED')", (reason, lid))
        for cid in campaigns:
            db.execute("UPDATE campaigns SET state='PAUSED',note=? WHERE id=? AND state='ACTIVE'", (reason, cid))

    def process_one(self):
        # Lock cobre elegibilidade, intenção durável e transporte: nenhuma edição ou opt-out concorre com envio.
        with self.store.lock:
            with self.store.db() as db:
                campaign = None
                for candidate in db.execute("SELECT * FROM campaigns WHERE state='ACTIVE' ORDER BY id"):
                    if candidate['expires_at'] <= stamp():
                        db.execute("UPDATE campaigns SET state='PAUSED',note='Autorização expirada.' WHERE id=?", (candidate['id'],))
                        continue
                    if candidate['last_dispatch_at'] and (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(candidate['last_dispatch_at'])).total_seconds() < candidate['interval_seconds']:
                        continue
                    campaign = dict(candidate)
                    break
                if not campaign:
                    return False
                cid = campaign['id']
                if campaign['automation_hash']:
                    from automation import settings
                    cfg = settings(self.store)
                    if not cfg['enabled'] or fingerprint(cfg) != campaign['automation_hash']:
                        db.execute("UPDATE campaigns SET state='PAUSED',note='Automação desativada ou roteiro alterado.' WHERE id=?", (cid,))
                        return False
                    last = db.execute('SELECT max(created_at) FROM deliveries').fetchone()[0]
                    if last and (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(last)).total_seconds() < cfg['interval_seconds']:
                        return False
                item = db.execute("SELECT * FROM campaign_items WHERE campaign_id=? AND state='QUEUED' ORDER BY id LIMIT 1", (cid,)).fetchone()
                if not item:
                    db.execute("UPDATE campaigns SET state='COMPLETED' WHERE id=?", (cid,))
                    return False
                item = dict(item)
                today = stamp()[:10]
                counts = db.execute("SELECT COUNT(*) FROM deliveries WHERE substr(created_at,1,10)=?", (today,)).fetchone()[0]
                manual = db.execute("SELECT COUNT(*) FROM interactions WHERE kind='CONTACT' AND substr(created_at,1,10)=? AND message_id NOT IN (SELECT message_id FROM campaign_items JOIN deliveries ON deliveries.item_id=campaign_items.id)", (today,)).fetchone()[0]
                campaign_count = db.execute('SELECT COUNT(*) FROM deliveries JOIN campaign_items ON campaign_items.id=deliveries.item_id WHERE campaign_id=? AND substr(deliveries.created_at,1,10)=?', (cid, today)).fetchone()[0]
                if counts + manual >= self.store.config['max_daily_actions'] or campaign_count >= campaign['daily_limit']:
                    return False
                msg = db.execute('SELECT * FROM messages WHERE id=?', (item['message_id'],)).fetchone()
                _, reason = self.eligible(db, item['lead_id'], reply=bool(msg and msg['variant'] == 'REPLY'))
                data = json.loads(self.store.get(db, item['lead_id'])['data'])
                if not reason and (msg['state'] != 'APPROVED' or msg['body'] != item['body'] or fingerprint(data) != item['evidence_hash']):
                    reason = 'Pesquisa, mensagem ou aprovação alterada.'
                if not reason and not self.adapter.status()['ready']:
                    reason = 'Integração indisponível ou desabilitada.'
                if not reason and not review_message(item['body'], data, reply=msg['variant'] == 'REPLY')['passed']:
                    reason = 'Revisão operacional bloqueou envio.'
                if reason:
                    self.invalidate(db, item['lead_id'], reason)
                    self.store.log(db, item['lead_id'], 'SEND_BLOCKED', reason)
                    return False
                if db.execute('SELECT 1 FROM deliveries WHERE item_id=?', (item['id'],)).fetchone():
                    raise ValueError('Item já possui tentativa durável.')
                db.execute("INSERT INTO deliveries(item_id,state,created_at,updated_at) VALUES (?,'SENDING',?,?)", (item['id'], stamp(), stamp()))
                db.execute("UPDATE campaign_items SET state='SENDING' WHERE id=?", (item['id'],))
                db.execute('UPDATE campaigns SET last_dispatch_at=? WHERE id=?', (stamp(), cid))
                self.store.log(db, item['lead_id'], 'SEND_STARTED', f'Campanha {cid}')
            try:
                provider_id = self.adapter.send(item['recipient_id'], item['body'])
            except Exception as error:
                uncertain = not isinstance(error, DeliveryError) or error.uncertain
                result = 'UNKNOWN' if uncertain else 'FAILED'
                detail = str(error) if isinstance(error, DeliveryError) else 'Resultado incerto; confira a conversa.'
                with self.store.db() as db:
                    db.execute('UPDATE deliveries SET state=?,updated_at=? WHERE item_id=?', (result, stamp(), item['id']))
                    db.execute('UPDATE campaign_items SET state=?,error=? WHERE id=?', (result, detail, item['id']))
                    db.execute("UPDATE campaigns SET state='PAUSED',note=? WHERE id=?", (detail, cid))
                    self.store.log(db, item['lead_id'], 'SEND_' + result, detail)
                    if campaign['automation_hash']:
                        db.execute("INSERT INTO automatic_contacts VALUES (?,1,'Falha no envio; conferir antes de retomar.') ON CONFLICT(recipient_id) DO UPDATE SET paused=1,reason=excluded.reason", (item['recipient_id'],))
                return False
            with self.store.db() as db:
                db.execute("UPDATE deliveries SET state='SENT',provider_id=?,updated_at=? WHERE item_id=?", (provider_id, stamp(), item['id']))
                db.execute("UPDATE campaign_items SET state='SENT' WHERE id=?", (item['id'],))
                db.execute("UPDATE messages SET state='USED' WHERE id=?", (item['message_id'],))
                db.execute("UPDATE messages SET state='SUPERSEDED' WHERE lead_id=? AND id<>? AND state IN ('PENDING','APPROVED')", (item['lead_id'], item['message_id']))
                db.execute("INSERT INTO interactions(lead_id,message_id,kind,note,created_at) VALUES (?,?,'CONTACT',?,?)", (item['lead_id'], item['message_id'], 'Instagram API: mensagem aceita pela Meta. Não implica leitura.', stamp()))
                db.execute("UPDATE leads SET status='CONTACTED',updated_at=? WHERE id=?", (stamp(), item['lead_id']))
                self.store.log(db, item['lead_id'], 'MESSAGE_SENT', f'Campanha {cid}; confirmação armazenada.')
            return True

    def recover(self):
        with self.store.db() as db:
            for row in db.execute("SELECT recipient_id FROM automatic_jobs WHERE state='PROCESSING'").fetchall():
                db.execute("INSERT INTO automatic_contacts VALUES (?,1,'Preparação interrompida; conferir antes de retomar.') ON CONFLICT(recipient_id) DO UPDATE SET paused=1,reason=excluded.reason", (row[0],))
            db.execute("UPDATE automatic_jobs SET state='FAILED',detail='Interrompido; revisão humana necessária.' WHERE state='PROCESSING'")
            for row in db.execute("SELECT item_id FROM deliveries WHERE state='SENDING'").fetchall():
                db.execute("INSERT INTO automatic_contacts(recipient_id,paused,reason) SELECT campaign_items.recipient_id,1,'Envio interrompido; conferir resultado.' FROM campaign_items JOIN campaigns ON campaigns.id=campaign_items.campaign_id WHERE campaign_items.id=? AND campaigns.automation_hash<>'' ON CONFLICT(recipient_id) DO UPDATE SET paused=1,reason=excluded.reason", (row[0],))
                db.execute("UPDATE deliveries SET state='UNKNOWN',updated_at=? WHERE item_id=?", (stamp(), row[0]))
                db.execute("UPDATE campaign_items SET state='UNKNOWN',error='Processo interrompido; conferir conversa.' WHERE id=?", (row[0],))
                db.execute("UPDATE campaigns SET state='PAUSED',note='Envio interrompido: resultado incerto.' WHERE id=(SELECT campaign_id FROM campaign_items WHERE id=?)", (row[0],))

    def reconcile(self, cid, payload):
        outcome = payload.get('outcome')
        if outcome not in ('sent', 'not_sent'):
            raise ValueError('Informe se a mensagem foi ou não enviada após conferir a conversa.')
        with self.store.db() as db:
            item = db.execute("SELECT * FROM campaign_items WHERE id=? AND campaign_id=? AND state='UNKNOWN'", (payload.get('item_id'), cid)).fetchone()
            if not item:
                raise ValueError('Item não possui resultado incerto para reconciliar.')
            state = 'CONFIRMED_MANUALLY' if outcome == 'sent' else 'NOT_SENT'
            db.execute('UPDATE campaign_items SET state=?,error=? WHERE id=?', (state, 'Conferido manualmente pelo operador.', item['id']))
            db.execute('UPDATE deliveries SET state=?,updated_at=? WHERE item_id=?', (state, stamp(), item['id']))
            if outcome == 'sent':
                db.execute("UPDATE messages SET state='USED' WHERE id=?", (item['message_id'],))
                db.execute("INSERT INTO interactions(lead_id,message_id,kind,note,created_at) VALUES (?,?,'CONTACT',?,?)", (item['lead_id'], item['message_id'], 'Envio conferido manualmente após resultado incerto; sem confirmação da API.', stamp()))
                db.execute("UPDATE leads SET status='CONTACTED',updated_at=? WHERE id=? AND do_not_contact=0", (stamp(), item['lead_id']))
            self.store.log(db, item['lead_id'], 'DELIVERY_RECONCILED', f'Campanha {cid}; {state}; conferência manual.')

    def run(self):
        self.recover()
        while not self.stop.wait(5):
            try:
                from automation import process
                process(self.store)
                self.process_one()
            except Exception:
                with self.store.db() as db:
                    db.execute("UPDATE campaigns SET state='PAUSED',note='Falha interna do worker.' WHERE state='ACTIVE'")
                    self.store.log(db, None, 'ERROR', 'Worker pausado por falha interna.')

    def receive(self, raw, signature):
        if not self.adapter.verify_signature(raw, signature):
            raise ValueError('Assinatura inválida.')
        payload = json.loads(raw)
        if not isinstance(payload, dict) or payload.get('object') != 'instagram':
            raise ValueError('Evento Instagram inválido.')
        account = self.adapter.env.get('INSTAGRAM_ACCOUNT_ID')
        processed = 0
        with self.store.db() as db:
            for entry in payload.get('entry', []):
                if str(entry.get('id')) != account:
                    continue
                for event in entry.get('messaging', []):
                    msg = event.get('message', {})
                    sender = str(event.get('sender', {}).get('id', ''))
                    if str(event.get('recipient', {}).get('id')) != account or not sender.isdigit() or sender == account or msg.get('is_echo') or not msg.get('mid'):
                        continue
                    timestamp = event.get('timestamp')
                    if not isinstance(timestamp, (int, float)):
                        continue
                    moment = dt.datetime.fromtimestamp(timestamp / 1000, dt.timezone.utc)
                    if moment > dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=30):
                        continue
                    mid = str(msg['mid'])[:500]
                    text = str(msg.get('text', '[Anexo recebido]'))[:4000]
                    if db.execute('SELECT 1 FROM inbox_events WHERE provider_id=?', (mid,)).fetchone():
                        continue
                    db.execute('INSERT INTO instagram_contacts(recipient_id,last_inbound_at) VALUES (?,?) ON CONFLICT(recipient_id) DO UPDATE SET last_inbound_at=MAX(last_inbound_at,excluded.last_inbound_at)', (sender, moment.isoformat()))
                    db.execute('INSERT INTO inbox_events(provider_id,recipient_id,body,occurred_at,created_at) VALUES (?,?,?,?,?)', (mid, sender, text, moment.isoformat(), stamp()))
                    contact = db.execute('SELECT lead_id FROM instagram_contacts WHERE recipient_id=?', (sender,)).fetchone()
                    lid = contact[0]
                    # Supressão conservadora; casos ambíguos ficam para revisão humana.
                    normalized = text.casefold().strip()
                    opted_out = bool(re.search(r'\b(stop|parar|pare|cancelar)\b|não (me )?(contat|mand|envi)|nao (me )?(contat|mand|envi)|sem interesse|não tenho interesse|nao tenho interesse', normalized))
                    if opted_out:
                        db.execute('UPDATE instagram_contacts SET suppressed=1 WHERE recipient_id=?', (sender,))
                    if lid:
                        self.invalidate(db, lid, 'Nova mensagem recebida: revisar contexto antes de enviar.')
                        db.execute("INSERT INTO interactions(lead_id,kind,note,created_at) VALUES (?,'STATUS',?,?)", (lid, 'REPLIED: ' + text, stamp()))
                        db.execute("UPDATE leads SET status='REPLIED',updated_at=? WHERE id=? AND do_not_contact=0", (stamp(), lid))
                        if opted_out:
                            db.execute("UPDATE leads SET do_not_contact=1,status='NOT_INTERESTED',updated_at=? WHERE id=?", (stamp(), lid))
                            db.execute("UPDATE messages SET state='CANCELLED' WHERE lead_id=? AND state IN ('PENDING','APPROVED')", (lid,))
                            db.execute("UPDATE followups SET state='CANCELLED' WHERE lead_id=? AND state IN ('PENDING','APPROVED')", (lid,))
                        else:
                            db.execute("UPDATE messages SET state='CANCELLED' WHERE id IN (SELECT message_id FROM followups WHERE lead_id=?) AND state IN ('PENDING','APPROVED')", (lid,))
                            db.execute("UPDATE followups SET state='CANCELLED' WHERE lead_id=? AND state IN ('PENDING','APPROVED')", (lid,))
                        self.store.log(db, lid, 'INSTAGRAM_RECEIVED', 'Mensagem recebida e contexto atualizado.')
                    processed += 1
                    if self.store.config.get('automatic_inbox', {}).get('enabled') and not opted_out:
                        db.execute('INSERT OR IGNORE INTO automatic_jobs(provider_id,recipient_id,created_at) VALUES (?,?,?)', (mid, sender, stamp()))
        return {'processed': processed}

    def link(self, recipient, lid):
        with self.store.db() as db:
            self.store.get(db, lid)
            contact = db.execute('SELECT * FROM instagram_contacts WHERE recipient_id=?', (recipient,)).fetchone()
            if not contact or contact['lead_id'] not in (None, lid):
                raise ValueError('Conversa indisponível para vínculo.')
            if db.execute('SELECT 1 FROM instagram_contacts WHERE lead_id=? AND recipient_id<>?', (lid, recipient)).fetchone():
                raise ValueError('Lead já possui outra conversa vinculada.')
            db.execute('UPDATE instagram_contacts SET lead_id=? WHERE recipient_id=?', (lid, recipient))
            lead = self.store.get(db, lid)
            if contact['suppressed'] or lead['do_not_contact']:
                db.execute("UPDATE leads SET do_not_contact=1,status='NOT_INTERESTED' WHERE id=?", (lid,))
                db.execute('UPDATE instagram_contacts SET suppressed=1 WHERE recipient_id=?', (recipient,))
                db.execute("UPDATE messages SET state='CANCELLED' WHERE lead_id=? AND state IN ('PENDING','APPROVED')", (lid,))
                self.invalidate(db, lid, 'Contato vinculado está bloqueado.')
            self.store.log(db, lid, 'INSTAGRAM_LINKED', 'Conversa vinculada pelo operador.')

    def prepare_reply(self, payload):
        lid = int(payload['lead_id'])
        body = str(payload.get('body', '')).strip()
        with self.store.db() as db:
            contact, reason = self.eligible(db, lid, reply=True)
            if reason:
                raise ValueError(reason)
            latest = db.execute('SELECT provider_id FROM inbox_events WHERE recipient_id=? ORDER BY occurred_at DESC,id DESC LIMIT 1', (contact['recipient_id'],)).fetchone()
            if not latest or latest[0] != payload.get('inbound_id'):
                raise ValueError('A conversa mudou. Atualize e revise a mensagem mais recente.')
            review = self.review(lid, body, reply=True)
            if not review['passed']:
                raise ValueError('; '.join(review['issues']))
            mid = db.execute("INSERT INTO messages(lead_id,variant,body,state,created_at) VALUES (?,'REPLY',?,'PENDING',?)", (lid, body, stamp())).lastrowid
            result = self.create({'name': 'Resposta à conversa', 'daily_limit': 1, 'interval_seconds': 60,
                                  'selections': [{'lead_id': lid, 'message_id': mid}]}, db=db)
            return {**result, 'review': review}

    def snapshot(self):
        with self.store.db() as db:
            campaigns = []
            for row in db.execute('SELECT * FROM campaigns ORDER BY id DESC'):
                c = dict(row)
                c['items'] = [dict(i) for i in db.execute('SELECT campaign_items.*,leads.company FROM campaign_items JOIN leads ON leads.id=campaign_items.lead_id WHERE campaign_id=?', (c['id'],))]
                for i in c['items']:
                    i['review'] = json.loads(i['review'])
                campaigns.append(c)
            contacts = []
            for row in db.execute('SELECT * FROM instagram_contacts ORDER BY last_inbound_at DESC'):
                c = dict(row)
                c['events'] = [dict(x) for x in db.execute('SELECT body,occurred_at,provider_id FROM inbox_events WHERE recipient_id=? ORDER BY occurred_at DESC,id DESC LIMIT 30', (c['recipient_id'],))]
                c['reply_reason'] = self.eligible(db, c['lead_id'], reply=True)[1] if c['lead_id'] else 'Vincule a conversa a um lead para responder.'
                auto = db.execute('SELECT paused,reason FROM automatic_contacts WHERE recipient_id=?', (c['recipient_id'],)).fetchone()
                c['automatic_paused'] = bool(auto and auto['paused'])
                c['automatic_reason'] = auto['reason'] if auto else ''
                contacts.append(c)
            eligibility = {str(r[0]): self.eligible(db, r[0])[1] for r in db.execute('SELECT id FROM leads')}
            return {'campaigns': campaigns, 'inbox': contacts, 'eligibility': eligibility,
                    'instagram': self.adapter.status(), 'ai': self.reviewer.status()}
