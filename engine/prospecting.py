"""Pesquisa pública de candidatos; ausência de resultado não comprova ausência de site."""
import html
import json
import os
import re
import urllib.parse
import urllib.request
from campaigns import stamp, review_message
from integrations import NoRedirect


def search(query, transport=None, store=None):
    token = os.getenv('TAVILY_API_KEY')
    if not token:
        raise ValueError('Configure TAVILY_API_KEY no .env para buscar perfis pelo Orbit.')
    if store is not None:
        with store.db() as db:
            count = db.execute('SELECT count(*) FROM search_requests WHERE substr(created_at,1,7)=?', (stamp()[:7],)).fetchone()[0]
            if count >= 100:
                raise ValueError('Limite local de 100 buscas por mês atingido. A captação aguarda o próximo mês.')
            db.execute('INSERT INTO search_requests(created_at) VALUES (?)', (stamp(),))
    payload = {'query': query, 'max_results': 20, 'search_depth': 'basic', 'auto_parameters': False,
               'include_domains': ['instagram.com'], 'include_answer': False, 'include_raw_content': False}
    request = urllib.request.Request('https://api.tavily.com/search', data=json.dumps(payload).encode(),
                                     headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token}, method='POST')
    try:
        with (transport or urllib.request.build_opener(NoRedirect()).open)(request, timeout=20) as response:
            result = json.loads(response.read(1_000_000))
        return [{'url': row.get('url', ''), 'title': row.get('title', ''), 'description': row.get('content', '')}
                for row in result.get('results', []) if isinstance(row, dict)]
    except Exception:
        raise ValueError('Pesquisa indisponível. Confira chave, conexão e disponibilidade do serviço.') from None


def profile(url):
    parsed = urllib.parse.urlsplit(str(url))
    if parsed.scheme != 'https' or parsed.hostname not in ('instagram.com', 'www.instagram.com'):
        return None
    parts = parsed.path.strip('/').split('/')
    if len(parts) != 1 or parts[0].casefold() in ('p', 'reel', 'reels', 'stories', 'explore', 'accounts', 'direct', 'about') or not re.fullmatch(r'[A-Za-z0-9_.]{1,30}', parts[0]):
        return None
    return parts[0].lower(), 'https://www.instagram.com/' + parts[0].lower() + '/'


def clean(value, limit):
    return html.unescape(re.sub(r'<[^>]*>', '', str(value or '')))[:limit]


def discover(store, payload, searcher=search):
    niche, city = str(payload.get('niche', '')).strip(), str(payload.get('city', '')).strip()
    if not niche or not city or len(niche) > 80 or len(city) > 80:
        raise ValueError('Informe nicho e cidade, com até 80 caracteres cada.')
    search_niche = {"agencia_marketing": "agência de marketing"}.get(niche, niche)
    query = f'site:instagram.com "{search_niche}" "{city}"'
    results = search(query, store=store) if searcher is search else searcher(query)
    if not isinstance(results, list):
        raise ValueError('Resposta de pesquisa inválida.')
    created, duplicates = 0, 0
    with store.db() as db:
        for result in results[:20]:
            if not isinstance(result, dict):
                continue
            identity = profile(result.get('url', ''))
            if not identity:
                continue
            username, url = identity
            if db.execute('SELECT 1 FROM prospect_candidates WHERE profile_url=?', (url,)).fetchone():
                duplicates += 1
                continue
            db.execute('INSERT INTO prospect_candidates(profile_url,username,company,niche,city,snippet,source,created_at) VALUES (?,?,?,?,?,?,?,?)', (url, username, clean(result.get('title'), 200) or '@'+username, niche, city, clean(result.get('description'), 1500), 'Resultado público de busca: ' + query, stamp()))
            created += 1
        store.log(db, None, 'PROSPECT_DISCOVERY', f'{created} candidatos; {duplicates} duplicados. Nenhuma DM enviada.')
    return {'created': created, 'duplicates': duplicates}


def update(store, payload):
    cid = int(payload['id'])
    with store.db() as db:
        row = db.execute('SELECT * FROM prospect_candidates WHERE id=?', (cid,)).fetchone()
        if not row:
            raise ValueError('Candidato não encontrado.')
        if payload.get('action') == 'reject':
            db.execute("UPDATE prospect_candidates SET state='REJECTED' WHERE id=?", (cid,))
            return {'id': cid}
        if payload.get('action') != 'review':
            raise ValueError('Ação inválida.')
        service = payload.get('service')
        note = str(payload.get('observation', '')).strip()
        source = str(payload.get('evidence_url', '')).strip()
        company = str(payload.get('company', row['company'])).strip()
        url = urllib.parse.urlsplit(source)
        if service not in ('LANDING_PAGE', 'WEBSITE', 'CONTENT') or not note or payload.get('verified') is not True:
            raise ValueError('Confirme uma observação real e o serviço relacionado.')
        if len(note) > 500 or not 1 <= len(company) <= 200 or len(source) > 2000 or url.scheme not in ('https', 'http') or not url.hostname:
            raise ValueError('Observação, empresa ou URL inválida.')
        hypothesis = {'LANDING_PAGE': 'Uma landing page pode facilitar a apresentação da oferta e o contato.', 'WEBSITE': 'Um site pode ajudar a apresentar seus serviços e orientar quem busca informações.', 'CONTENT': 'Posts com uma mensagem e um próximo passo claros podem ajudar na comunicação da oferta.'}[service]
        body = f'Olá, equipe da {company}! Ao conferir seu perfil, observei: {note} {hypothesis} Posso compartilhar uma sugestão? — Agência Santx'
        review = review_message(body, {'evidence': {'prospect': {'verified': True, 'source': source, 'note': note}}})
        if not review['passed']:
            raise ValueError('; '.join(review['issues']))
        db.execute("UPDATE prospect_candidates SET company=?,service=?,observation=?,evidence_url=?,approach=?,state='READY' WHERE id=?", (company, service, note, source, body, cid))
        store.log(db, None, 'PROSPECT_REVIEWED', f'Candidato {cid}: abordagem preparada, sem envio automático.')
    lead = store.create({'company': company, 'instagram': row['profile_url'], 'niche': row['niche'], 'city': row['city'], 'description': row['snippet'], 'source': source, 'offer': service, 'evidence': {'prospect': {'verified': True, 'source': source, 'note': note}}})
    return {'id': cid, 'lead_id': lead['id'], 'lead_created': not lead.get('duplicate', False)}


def snapshot(store):
    with store.db() as db:
        used = db.execute('SELECT count(*) FROM search_requests WHERE substr(created_at,1,7)=?', (stamp()[:7],)).fetchone()[0]
        candidates = []
        for row in db.execute("SELECT * FROM prospect_candidates WHERE state<>'REJECTED' ORDER BY CASE state WHEN 'READY' THEN 0 WHEN 'RESEARCH' THEN 1 ELSE 2 END, id DESC LIMIT 200"):
            candidate = dict(row)
            candidate['research'] = json.loads(candidate.pop('research_json'))
            candidates.append(candidate)
        return {'candidates': candidates, 'configured': bool(os.getenv('TAVILY_API_KEY')),
                'provider': 'Tavily', 'monthly_limit': 100, 'remaining_searches': max(0,100-used),
                'schedule': store.config.get('prospecting_schedule', {'enabled': False, 'city': 'Manaus'}),
                'runs': [dict(row) for row in db.execute('SELECT * FROM prospect_search_runs ORDER BY id DESC LIMIT 10')]}


def configure_schedule(store, payload):
    enabled = payload.get('enabled')
    city = str(payload.get('city', '')).strip()
    if type(enabled) is not bool or not 1 <= len(city) <= 80:
        raise ValueError('Informe ativação e cidade válidas.')
    if enabled and not os.getenv('TAVILY_API_KEY'):
        raise ValueError('Conecte a API de busca antes de ativar a captação diária.')
    from pathlib import Path
    from app import ROOT
    with store.lock:
        proposed = {**store.config, 'prospecting_schedule': {'enabled': enabled, 'city': city}}
        path = Path(os.getenv('CONFIG_PATH', ROOT / 'config.json'))
        temporary = path.with_suffix('.json.tmp')
        temporary.write_text(json.dumps(proposed, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(path)
        store.config = proposed
        with store.db() as db:
            store.log(db, None, 'PROSPECT_SCHEDULE', 'Captação diária ativada.' if enabled else 'Captação diária desativada.')
    return proposed['prospecting_schedule']


def process_schedule(store, searcher=search):
    with store.db() as db:
        cfg = store.config.get('prospecting_schedule', {})
        if not cfg.get('enabled') or not os.getenv('TAVILY_API_KEY'):
            return False
        city = cfg['city']
        for niche in ('advogado', 'médico'):
            existing = db.execute('SELECT 1 FROM prospect_search_runs WHERE day=? AND niche=? AND city=?', (stamp()[:10], niche, city)).fetchone()
            if not existing:
                run = db.execute("INSERT INTO prospect_search_runs(day,niche,city,state,created_at) VALUES (?,?,?,'PROCESSING',?)", (stamp()[:10], niche, city, stamp())).lastrowid
                break
        else:
            return False
    try:
        result = discover(store, {'niche': niche, 'city': city}, searcher=searcher)
        with store.db() as db:
            db.execute("UPDATE prospect_search_runs SET state='COMPLETED',detail=? WHERE id=?", (str(result['created'])+' candidatos encontrados.', run))
        return True
    except Exception:
        with store.db() as db:
            db.execute("UPDATE prospect_search_runs SET state='FAILED',detail='Busca indisponível; confira configuração e conexão.' WHERE id=?", (run,))
            store.log(db, None, 'PROSPECT_SEARCH_FAILED', 'Busca diária indisponível; sem repetição automática no mesmo dia.')
        return False


def run(store):
    with store.db() as db:
        db.execute("UPDATE prospect_search_runs SET state='FAILED',detail='Busca interrompida; sem repetição automática no mesmo dia.' WHERE state='PROCESSING'")
    while not store.campaigns.stop.wait(60):
        try:
            process_schedule(store)
        except Exception:
            with store.db() as db:
                store.log(db, None, 'PROSPECT_SEARCH_FAILED', 'Falha interna na busca diária.')
