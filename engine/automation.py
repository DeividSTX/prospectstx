"""Atendimento automático com roteiro aprovado, fila durável e revisão local."""
import datetime as dt
import json
import re

from campaigns import fingerprint, stamp


DEFAULTS = {'enabled': False, 'agency': 'Agência Santx', 'services': '',
            'question': 'Qual é o nome da sua empresa e o que você gostaria de melhorar?',
            'handoff': 'Vou encaminhar sua solicitação para a equipe. O atendimento humano responderá assim que estiver disponível.',
            'per_contact_limit': 5, 'interval_seconds': 60}


def settings(store):
    return {**DEFAULTS, **store.config.get('automatic_inbox', {})}


def configure(store, payload):
    value = {**settings(store), **{k: v for k, v in payload.items() if k in DEFAULTS}}
    if type(value['enabled']) is not bool:
        raise ValueError('Ativação inválida.')
    for key in ('agency', 'services', 'question', 'handoff'):
        if not isinstance(value[key], str) or len(value[key]) > 280:
            raise ValueError('Textos do roteiro devem ter até 280 caracteres.')
        value[key] = value[key].strip()
    for key, low, high in [('per_contact_limit', 1, 10), ('interval_seconds', 30, 3600)]:
        if type(value[key]) is not int or not low <= value[key] <= high:
            raise ValueError('Limites do atendimento inválidos.')
    if value['enabled'] and (not all(value[k] for k in ('agency', 'services', 'question', 'handoff')) or not store.campaigns.adapter.status()['ready']):
        raise ValueError('Preencha o roteiro e configure o Instagram antes de ativar.')
    from pathlib import Path
    import os
    from app import ROOT
    with store.lock:
        config = {**store.config, 'automatic_inbox': value}
        path = Path(os.getenv('CONFIG_PATH', ROOT / 'config.json'))
        temporary = path.with_suffix('.json.tmp')
        temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(path)
        store.config = config
        with store.db() as db:
            db.execute("UPDATE campaigns SET state='PAUSED',note='Roteiro automático alterado.' WHERE automation_hash<>'' AND state='ACTIVE'")
            db.execute("UPDATE automatic_jobs SET state='CANCELLED',detail='Roteiro alterado.' WHERE state='QUEUED'")
            store.log(db, None, 'AUTOMATION_SETTINGS', 'Atendimento automático ativado.' if value['enabled'] else 'Atendimento automático desativado.')
    return value


def pause_contact(store, recipient, paused):
    if type(paused) is not bool:
        raise ValueError('Estado inválido.')
    with store.db() as db:
        if not db.execute('SELECT 1 FROM instagram_contacts WHERE recipient_id=?', (recipient,)).fetchone():
            raise ValueError('Conversa não encontrada.')
        db.execute('INSERT INTO automatic_contacts(recipient_id,paused,reason) VALUES (?,?,?) ON CONFLICT(recipient_id) DO UPDATE SET paused=excluded.paused,reason=excluded.reason', (recipient, int(paused), 'Pausado pelo operador.' if paused else ''))
        db.execute("UPDATE automatic_jobs SET state='CANCELLED' WHERE recipient_id=? AND state='QUEUED'", (recipient,))
        db.execute("UPDATE campaigns SET state='PAUSED',note='Controle humano da conversa.' WHERE automation_hash<>'' AND id IN (SELECT campaign_id FROM campaign_items WHERE recipient_id=? AND state IN ('READY','QUEUED'))", (recipient,))
        store.log(db, None, 'AUTOMATION_CONTACT', 'Atendimento pausado.' if paused else 'Atendimento retomado para novas mensagens.')


def process(store):
    service = store.campaigns
    with store.lock:
        cfg = settings(store)
        if not cfg['enabled'] or not service.adapter.status()['ready']:
            return False
        with store.db() as db:
            job = db.execute("SELECT automatic_jobs.*,inbox_events.body FROM automatic_jobs JOIN inbox_events USING(provider_id) WHERE state='QUEUED' ORDER BY automatic_jobs.id LIMIT 1").fetchone()
            if not job:
                return False
            job = dict(job)
            recipient = job['recipient_id']
            contact = db.execute('SELECT * FROM instagram_contacts WHERE recipient_id=?', (recipient,)).fetchone()
            age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(contact['last_inbound_at'])
            if age.total_seconds() < 10:
                return False
            latest = db.execute('SELECT provider_id FROM inbox_events WHERE recipient_id=? ORDER BY occurred_at DESC,id DESC LIMIT 1', (recipient,)).fetchone()[0]
            paused = db.execute('SELECT paused FROM automatic_contacts WHERE recipient_id=?', (recipient,)).fetchone()
            if latest != job['provider_id'] or contact['suppressed'] or (paused and paused[0]) or age >= dt.timedelta(hours=24):
                db.execute("UPDATE automatic_jobs SET state='CANCELLED',detail='Contexto alterado, bloqueado ou expirado.' WHERE id=?", (job['id'],))
                return False
            count = db.execute("SELECT count(*) FROM automatic_jobs WHERE recipient_id=? AND state IN ('PROCESSING','PREPARED','FAILED') AND substr(created_at,1,10)=?", (recipient, stamp()[:10])).fetchone()[0]
            if count >= cfg['per_contact_limit']:
                db.execute("UPDATE automatic_jobs SET state='CANCELLED',detail='Limite por conversa atingido.' WHERE id=?", (job['id'],))
                db.execute("INSERT INTO automatic_contacts VALUES (?,1,'Limite por conversa atingido.') ON CONFLICT(recipient_id) DO UPDATE SET paused=1,reason=excluded.reason", (recipient,))
                return False
            db.execute("UPDATE automatic_jobs SET state='PROCESSING' WHERE id=?", (job['id'],))
            lid = contact['lead_id']
        try:
            if not lid:
                lid = store.create({'company': 'Contato Instagram ' + recipient, 'source': 'Conversa recebida pelo Instagram'})['id']
                service.link(recipient, lid)
            text = job['body'].casefold()
            human = bool(re.search(r'humano|atendente|reclama|reembolso|cancelamento|advogado|processo|senha|cartão|cartao|documento|preço|preco|valor|quanto|orçamento|orcamento|como.*funcion|funcionaria', text)) or text == '[anexo recebido]'
            with store.db() as db:
                previous = db.execute("SELECT count(*) FROM automatic_jobs WHERE recipient_id=? AND state='PREPARED'", (recipient,)).fetchone()[0]
            if human or previous >= 2:
                body, handoff = cfg['handoff'], True
            elif re.search(r'preço|preco|valor|quanto|orçamento|orcamento|serviço|servico', text):
                body, handoff = f"Somos o atendimento automático da {cfg['agency']}. Oferecemos: {cfg['services']}. Para entender sua necessidade: {cfg['question']}", False
            elif previous == 0:
                body, handoff = f"Olá! Sou o atendimento automático da {cfg['agency']}. {cfg['question']}", False
            else:
                body, handoff = f"Obrigado pelas informações. {cfg['services']}. Qual desses serviços você procura e qual é sua principal dúvida?", False
            result = service.prepare_reply({'lead_id': lid, 'inbound_id': job['provider_id'], 'body': body})
            with store.db() as db:
                db.execute('UPDATE campaigns SET automation_hash=? WHERE id=?', (fingerprint(cfg), result['id']))
                db.execute("UPDATE automatic_jobs SET state='PREPARED',campaign_id=? WHERE id=?", (result['id'], job['id']))
            service.change(result['id'], 'authorize')
            if handoff:
                with store.db() as db:
                    db.execute("INSERT INTO automatic_contacts VALUES (?,1,'Solicitação encaminhada para atendimento humano.') ON CONFLICT(recipient_id) DO UPDATE SET paused=1,reason=excluded.reason", (recipient,))
                    store.log(db, lid, 'HUMAN_HANDOFF', 'Conversa encaminhada para atendimento humano.')
            return True
        except Exception:
            with store.db() as db:
                db.execute("UPDATE automatic_jobs SET state='FAILED',detail='Revisão ou preparação falhou; confira a conversa.' WHERE id=?", (job['id'],))
                if 'result' in locals():
                    db.execute("UPDATE campaigns SET state='PAUSED',note='Preparação automática interrompida.' WHERE id=?", (result['id'],))
                db.execute("INSERT INTO automatic_contacts VALUES (?,1,'Falha no atendimento automático; revisão humana necessária.') ON CONFLICT(recipient_id) DO UPDATE SET paused=1,reason=excluded.reason", (recipient,))
                store.log(db, lid, 'AUTOMATION_FAILED', 'Atendimento pausado para revisão humana.')
            return False
