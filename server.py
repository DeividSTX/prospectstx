"""Orbit 2: administração central e bancos separados por agência."""
import argparse
import datetime as dt
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'engine'))
from app import Store, handler as engine_handler
from auth import password_hash

COOKIE = 'orbit_v2_session'
TZ = dt.timezone(dt.timedelta(hours=-3))


def today():
    return dt.datetime.now(TZ).date().isoformat()


def stamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def text(value, limit=3000):
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError('Texto inválido.')
    return value.strip()


def date(value):
    value = text(value, 10)
    if dt.date.fromisoformat(value).isoformat() != value:
        raise ValueError('Data inválida.')
    return value


def money(value):
    if type(value) is not int or not 0 <= value <= 100_000_000:
        raise ValueError('Valor monetário inválido.')
    return value


class Control:
    def __init__(self, root=None):
        self.root = Path(root or ROOT / 'data')
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.stores = {}
        with self.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS agencies(
              id INTEGER PRIMARY KEY, name TEXT NOT NULL, contact TEXT NOT NULL,
              enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS subscriptions(
              agency_id INTEGER PRIMARY KEY REFERENCES agencies(id), plan TEXT NOT NULL,
              price_cents INTEGER NOT NULL, status TEXT NOT NULL, valid_until TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS users(
              id INTEGER PRIMARY KEY, agency_id INTEGER REFERENCES agencies(id),
              email TEXT NOT NULL UNIQUE, digest TEXT NOT NULL, role TEXT NOT NULL,
              enabled INTEGER NOT NULL DEFAULT 1, last_login TEXT);
            CREATE TABLE IF NOT EXISTS sessions(
              digest TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), expires_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS attempts(bucket TEXT NOT NULL, attempted_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS payments(
              id INTEGER PRIMARY KEY, agency_id INTEGER NOT NULL REFERENCES agencies(id),
              amount_cents INTEGER NOT NULL, paid_on TEXT NOT NULL, reference TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'PAID', created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS audit(
              id INTEGER PRIMARY KEY, actor INTEGER, action TEXT NOT NULL,
              agency_id INTEGER, detail TEXT NOT NULL, created_at TEXT NOT NULL);
            ''')

    @contextmanager
    def db(self):
        with self.lock:
            if os.getenv('DATABASE_URL'):
                from cloud_db import postgres_db
                with postgres_db('prospectstx_control') as db:
                    yield db
                return
            db = sqlite3.connect(self.root / 'control.db')
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            try:
                with db:
                    yield db
            finally:
                db.close()

    def audit(self, db, actor, action, agency_id=None, detail=''):
        db.execute('INSERT INTO audit(actor,action,agency_id,detail,created_at) VALUES (?,?,?,?,?)',
                   (actor, action, agency_id, detail, stamp()))

    def bootstrap(self, email, password):
        email = self.email(email)
        digest = password_hash(password)
        with self.db() as db:
            if db.execute("SELECT 1 FROM users WHERE role='ADMIN'").fetchone():
                raise ValueError('Administrador já cadastrado.')
            db.execute("INSERT INTO users(email,digest,role) VALUES (?,?,'ADMIN')", (email, digest))
            self.audit(db, None, 'ADMIN_CREATED')

    @staticmethod
    def email(value):
        value = text(value, 254).casefold()
        if '@' not in value or '.' not in value.split('@')[-1] or any(c.isspace() for c in value):
            raise ValueError('E-mail inválido.')
        return value

    def change_admin(self, email, password):
        email = self.email(email)
        if not isinstance(password, str) or len(password) > 1024:
            raise ValueError('Senha inválida.')
        digest = password_hash(password)
        with self.db() as db:
            admins = db.execute("SELECT id FROM users WHERE role='ADMIN'").fetchall()
            if len(admins) != 1:
                raise ValueError('É necessário ter exatamente um administrador cadastrado.')
            uid = admins[0]['id']
            if db.execute('SELECT 1 FROM users WHERE email=? AND id<>?', (email, uid)).fetchone():
                raise ValueError('Este e-mail já pertence a outro usuário.')
            db.execute('UPDATE users SET email=?,digest=?,enabled=1 WHERE id=?', (email, digest, uid))
            db.execute('DELETE FROM sessions WHERE user_id=?', (uid,))
            self.audit(db, uid, 'ADMIN_CHANGED')

    @staticmethod
    def access(row):
        return bool(row['enabled'] and row['agency_enabled'] and row['subscription_status'] in ('ACTIVE', 'TRIAL')
                    and row['valid_until'] >= today())

    def principal(self, cookie):
        try:
            jar = SimpleCookie(); jar.load(cookie or '')
            token = jar[COOKIE].value if COOKIE in jar else ''
        except Exception:
            return None
        if len(token) != 43:
            return None
        with self.db() as db:
            row = db.execute('''SELECT u.id,u.email,u.role,u.agency_id,u.enabled,
                a.enabled agency_enabled,s.status subscription_status,s.valid_until
                FROM sessions x JOIN users u ON u.id=x.user_id
                LEFT JOIN agencies a ON a.id=u.agency_id
                LEFT JOIN subscriptions s ON s.agency_id=a.id
                WHERE x.digest=? AND x.expires_at>?''',
                (hashlib.sha256(token.encode()).hexdigest(), int(time.time()))).fetchone()
            if not row or not row['enabled']:
                return None
            return dict(row)

    def login(self, email, password, address):
        email = text(email, 254).casefold()
        if not isinstance(password,str) or len(password)>1024:
            return None,401
        current = int(time.time()); bucket = hashlib.sha256(address.encode()).hexdigest()
        with self.db() as db:
            db.execute('DELETE FROM attempts WHERE attempted_at<?', (current-900,))
            if db.execute('SELECT count(*) FROM attempts WHERE bucket=?', (bucket,)).fetchone()[0] >= 5:
                return None, 429
            db.execute('INSERT INTO attempts VALUES (?,?)', (bucket, current))
            user = db.execute('''SELECT u.*,a.enabled agency_enabled,s.status subscription_status,s.valid_until
                FROM users u LEFT JOIN agencies a ON a.id=u.agency_id
                LEFT JOIN subscriptions s ON s.agency_id=a.id WHERE u.email=?''', (email,)).fetchone()
        # Igual custo de derivação para usuários inexistentes.
        digest = user['digest'] if user else 'pbkdf2_sha256$600000$'+'00'*16+'$'+'00'*32
        _, rounds, salt, expected = digest.split('$')
        actual = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), int(rounds)).hex()
        if not user or not hmac.compare_digest(actual, expected) or not user['enabled']:
            return None, 401
        if user['role'] != 'ADMIN' and not self.access(user):
            return None, 403
        token = secrets.token_urlsafe(32)
        with self.db() as db:
            db.execute('DELETE FROM attempts WHERE bucket=?', (bucket,))
            db.execute('DELETE FROM sessions WHERE expires_at<=?', (current,))
            db.execute('INSERT INTO sessions VALUES (?,?,?)', (hashlib.sha256(token.encode()).hexdigest(), user['id'], current+43200))
            db.execute('UPDATE users SET last_login=? WHERE id=?', (stamp(), user['id']))
            self.audit(db, user['id'], 'LOGIN', user['agency_id'])
        return token, 200

    def logout(self, cookie):
        jar = SimpleCookie(); jar.load(cookie or '')
        if COOKIE in jar:
            with self.db() as db:
                db.execute('DELETE FROM sessions WHERE digest=?', (hashlib.sha256(jar[COOKIE].value.encode()).hexdigest(),))

    def create_agency(self, actor, payload):
        name = text(payload.get('name', ''), 80)
        contact = text(payload.get('contact', ''), 254)
        if not name:
            raise ValueError('Informe o nome da agência.')
        email = self.email(payload.get('email', ''))
        digest = password_hash(payload.get('password'))
        plan = text(payload.get('plan', 'ProspectSTX Inicial'), 80)
        price = money(payload.get('price_cents', 19700))
        status = payload.get('status', 'TRIAL')
        if status not in ('TRIAL', 'ACTIVE', 'CANCELLED'):
            raise ValueError('Situação da assinatura inválida.')
        until = date(payload.get('valid_until', ''))
        with self.db() as db:
            if db.execute('SELECT 1 FROM users WHERE email=?', (email,)).fetchone():
                raise ValueError('Este login já existe.')
            aid = db.execute('INSERT INTO agencies(name,contact,created_at) VALUES (?,?,?)', (name, contact, stamp())).lastrowid
            db.execute('INSERT INTO subscriptions VALUES (?,?,?,?,?)', (aid, plan, price, status, until))
            db.execute("INSERT INTO users(agency_id,email,digest,role) VALUES (?,?,?,'AGENCY')", (aid, email, digest))
            self.audit(db, actor, 'AGENCY_CREATED', aid)
        return {'id': aid}

    def subscription(self, actor, payload):
        aid = int(payload['agency_id']); status = payload.get('status')
        if status not in ('TRIAL', 'ACTIVE', 'CANCELLED'):
            raise ValueError('Situação inválida.')
        until = date(payload['valid_until']); price = money(payload['price_cents'])
        plan = text(payload['plan'], 80)
        with self.db() as db:
            if not db.execute('SELECT 1 FROM agencies WHERE id=?', (aid,)).fetchone():
                raise ValueError('Agência não encontrada.')
            db.execute('UPDATE subscriptions SET plan=?,price_cents=?,status=?,valid_until=? WHERE agency_id=?', (plan, price, status, until, aid))
            self.audit(db, actor, 'SUBSCRIPTION_UPDATED', aid, json.dumps({'status': status, 'valid_until': until}))
        return {'ok': True}

    def toggle(self, actor, payload):
        aid = int(payload['agency_id']); enabled = payload['enabled']
        if type(enabled) is not bool:
            raise ValueError('Acesso inválido.')
        with self.db() as db:
            if db.execute('UPDATE agencies SET enabled=? WHERE id=?', (int(enabled), aid)).rowcount != 1:
                raise ValueError('Agência não encontrada.')
            db.execute('DELETE FROM sessions WHERE user_id IN (SELECT id FROM users WHERE agency_id=?)', (aid,))
            self.audit(db, actor, 'ACCESS_ENABLED' if enabled else 'ACCESS_SUSPENDED', aid)
        return {'ok': True}

    def reset_password(self, actor, payload):
        aid = int(payload['agency_id']); digest = password_hash(payload['password'])
        with self.db() as db:
            if db.execute("UPDATE users SET digest=? WHERE agency_id=? AND role='AGENCY'", (digest, aid)).rowcount != 1:
                raise ValueError('Login não encontrado.')
            db.execute('DELETE FROM sessions WHERE user_id IN (SELECT id FROM users WHERE agency_id=?)', (aid,))
            self.audit(db, actor, 'PASSWORD_RESET', aid)
        return {'ok': True}

    def payment(self, actor, payload):
        aid = int(payload['agency_id']); amount = money(payload['amount_cents'])
        if amount == 0:
            raise ValueError('Informe um valor maior que zero.')
        paid_on = date(payload['paid_on']); reference = text(payload.get('reference', ''), 254)
        if paid_on > today() or not reference:
            raise ValueError('Informe uma data de pagamento já ocorrida e uma referência do comprovante.')
        with self.db() as db:
            if not db.execute('SELECT 1 FROM agencies WHERE id=?', (aid,)).fetchone():
                raise ValueError('Agência não encontrada.')
            if db.execute("SELECT 1 FROM payments WHERE agency_id=? AND reference=? AND status='PAID'", (aid, reference)).fetchone():
                raise ValueError('Comprovante já registrado para esta agência.')
            pid = db.execute('INSERT INTO payments(agency_id,amount_cents,paid_on,reference,created_at) VALUES (?,?,?,?,?)', (aid, amount, paid_on, reference, stamp())).lastrowid
            self.audit(db, actor, 'PAYMENT_RECORDED', aid, str(pid))
        return {'id': pid}

    def void_payment(self, actor, payload):
        pid = int(payload['payment_id'])
        reason = text(payload.get('reason', ''), 500)
        if not reason:
            raise ValueError('Informe o motivo do estorno do registro.')
        with self.db() as db:
            row = db.execute("SELECT agency_id FROM payments WHERE id=? AND status='PAID'", (pid,)).fetchone()
            if not row:
                raise ValueError('Pagamento não encontrado ou já estornado.')
            db.execute("UPDATE payments SET status='VOID' WHERE id=?", (pid,))
            self.audit(db, actor, 'PAYMENT_VOIDED', row[0], json.dumps({'payment_id': pid, 'reason': reason}))
        return {'ok': True}

    def snapshot(self):
        with self.db() as db:
            agencies = [dict(r) for r in db.execute('''SELECT a.*,s.plan,s.price_cents,s.status,s.valid_until,
              u.email,u.last_login FROM agencies a JOIN subscriptions s ON s.agency_id=a.id
              JOIN users u ON u.agency_id=a.id ORDER BY a.id DESC''')]
            payments = [dict(r) for r in db.execute('SELECT p.*,a.name agency_name FROM payments p JOIN agencies a ON a.id=p.agency_id ORDER BY p.id DESC')]
            logs = [dict(r) for r in db.execute('SELECT * FROM audit ORDER BY id DESC LIMIT 100')]
        for a in agencies:
            a['access_allowed'] = bool(a['enabled'] and a['status'] in ('ACTIVE', 'TRIAL') and a['valid_until'] >= today())
        active = [a for a in agencies if a['enabled'] and a['status']=='ACTIVE' and a['valid_until']>=today()]
        paid = [p for p in payments if p['status']=='PAID']
        metrics = {'agencies':len(agencies), 'active_subscriptions':len(active),
          'paying_agencies':len({p['agency_id'] for p in paid}),
          'used_30_days':sum(bool(a['last_login'] and a['last_login'][:10] >= (dt.date.fromisoformat(today())-dt.timedelta(days=30)).isoformat()) for a in agencies),
          'received_cents':sum(p['amount_cents'] for p in paid),
          'month_received_cents':sum(p['amount_cents'] for p in paid if p['paid_on'][:7]==today()[:7]),
          'contracted_monthly_cents':sum(a['price_cents'] for a in active)}
        return {'agencies':agencies,'payments':payments,'logs':logs,'metrics':metrics,'today':today()}

    def tenant_store(self, aid):
        with self.lock:
            if aid not in self.stores:
                with self.db() as db:
                    row = db.execute('SELECT name FROM agencies WHERE id=?', (aid,)).fetchone()
                config = json.loads((ROOT/'engine/config.json').read_text(encoding='utf-8'))
                config.update(agency_name=row[0], mode='MANUAL')
                if os.getenv('DATABASE_URL'):
                    from cloud_store import CloudStore
                    store = CloudStore(aid, config)
                else:
                    store = Store(self.root/'tenants'/str(aid)/'orbit.db', config)
                store.campaigns.adapter.env = {}  # Nunca compartilhar credenciais de integração.
                self.stores[aid] = store
            return self.stores[aid]


def make_handler(control, public_url=''):
    parsed = urlsplit(public_url)
    backend = urlsplit(os.getenv('ORBIT_V2_BACKEND_URL',''))
    if backend.netloc and (backend.scheme != 'https' or backend.path not in ('','/') or backend.query or backend.fragment or backend.username):
        raise ValueError('URL do backend inválida.')
    if public_url and (parsed.scheme != 'https' or not parsed.netloc or parsed.path not in ('','/') or parsed.query or parsed.fragment or parsed.username):
        raise ValueError('Use uma URL pública HTTPS válida.')
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(30)

        def log_message(self, *args):
            pass

        def respond(self, status, data, mime='application/json; charset=utf-8', headers=None):
            body = data if isinstance(data,bytes) else json.dumps(data,ensure_ascii=False).encode()
            self.send_response(status)
            for key,value in {'Content-Type':mime,'Content-Length':str(len(body)),'Cache-Control':'no-store',
              'X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer',
              'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'", **(headers or {})}.items():
                self.send_header(key,value)
            self.end_headers(); self.wfile.write(body)

        def hosts(self):
            return {url.netloc for url in (parsed,backend) if url.netloc} if public_url else {f'localhost:{self.server.server_port}', f'127.0.0.1:{self.server.server_port}'}

        def valid_host(self):
            return len(self.headers.get_all('Host',[]))==1 and self.headers.get('Host') in self.hosts()

        def cookie(self, token=''):
            return f'{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={43200 if token else 0}'+('; Secure' if public_url else '')

        def principal(self):
            return control.principal(self.headers.get('Cookie'))

        def do_GET(self):
            if not self.valid_host():
                return self.respond(403,{'error':'Host não permitido.'})
            path = urlsplit(self.path).path
            files = {'/login':(ROOT/'engine/static/login.html','text/html'), '/login.js':(ROOT/'engine/static/login.js','text/javascript'),
                     '/style.css':(ROOT/'engine/static/style.css','text/css')}
            if path in files:
                file,mime=files[path]; return self.respond(200,file.read_bytes(),mime+'; charset=utf-8')
            if path=='/healthz': return self.respond(200,{'ok':True})
            user=self.principal()
            if not user:
                return self.respond(302 if path in ('/','/admin') else 401,{'error':'Faça login.'},headers={'Location':'/login'} if path in ('/','/admin') else None)
            if user['role']=='ADMIN':
                if path=='/': return self.respond(302,{},headers={'Location':'/admin'})
                if path=='/api/admin/state': return self.respond(200,control.snapshot())
                if path in ('/admin','/admin.js'):
                    file=ROOT/'static'/('admin.html' if path=='/admin' else 'admin.js')
                    return self.respond(200,file.read_bytes(),('text/html' if path=='/admin' else 'text/javascript')+'; charset=utf-8')
                return self.respond(404,{'error':'Não encontrado.'})
            if path.startswith('/admin') or path.startswith('/api/admin'):
                return self.respond(403,{'error':'Acesso exclusivo do administrador.'})
            if not control.access(user): return self.respond(403,{'error':'Acesso suspenso ou assinatura sem vigência.'})
            self.delegate(user,'GET')

        def do_POST(self):
            origin=self.headers.get('Origin');allowed={public_url.rstrip('/')} if public_url else {f'http://{h}' for h in self.hosts()}
            if not self.valid_host() or (origin and origin not in allowed) or self.headers.get('X-Requested-With')!='ProspectLocal':
                return self.respond(403,{'error':'Origem não permitida.'})
            if len(self.headers.get_all('Content-Length',[]))!=1 or self.headers.get('Transfer-Encoding'):
                return self.respond(400,{'error':'Requisição inválida.'})
            path=urlsplit(self.path).path;user=self.principal()
            if path!='/api/auth/login' and not user: return self.respond(401,{'error':'Faça login.'})
            if user and user['role']!='ADMIN' and not path.startswith('/api/auth/'):
                if path.startswith('/api/admin'): return self.respond(403,{'error':'Acesso exclusivo do administrador.'})
                if not control.access(user): return self.respond(403,{'error':'Acesso suspenso ou assinatura sem vigência.'})
                # Configurações globais e integrações aguardam implementação por agência.
                if path.startswith(('/api/settings','/api/prospecting/','/api/automation/','/api/campaigns','/api/inbox/')):
                    return self.respond(409,{'error':'Integrações e configurações por agência serão habilitadas na próxima etapa.'})
                return self.delegate(user,'POST')
            try:
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<=16384: raise ValueError('Formulário excede o limite.')
                payload=json.loads(self.rfile.read(size))
                if not isinstance(payload,dict): raise ValueError('Objeto JSON obrigatório.')
                if path=='/api/auth/login':
                    token,status=control.login(payload.get('user',''),payload.get('password',''),self.client_address[0])
                    if status!=200:return self.respond(status,{'error':{401:'Login ou senha inválidos.',403:'Acesso suspenso ou assinatura sem vigência.',429:'Muitas tentativas. Aguarde 15 minutos.'}[status]})
                    return self.respond(200,{'ok':True},headers={'Set-Cookie':self.cookie(token)})
                if path=='/api/auth/logout':
                    control.logout(self.headers.get('Cookie'));return self.respond(200,{'ok':True},headers={'Set-Cookie':self.cookie()})
                actions={'/api/admin/agencies':control.create_agency,'/api/admin/subscription':control.subscription,
                  '/api/admin/access':control.toggle,'/api/admin/password':control.reset_password,
                  '/api/admin/payments':control.payment,'/api/admin/payments/void':control.void_payment}
                if path not in actions:return self.respond(404,{'error':'Não encontrado.'})
                if user['role']!='ADMIN':return self.respond(403,{'error':'Acesso exclusivo do administrador.'})
                return self.respond(200,{'ok':True,'result':actions[path](user['id'],payload)})
            except (ValueError,TypeError,KeyError,OverflowError):
                return self.respond(400,{'error':'Dados inválidos. Confira datas, valores, senha (12 caracteres) e campos obrigatórios.'})
            except sqlite3.IntegrityError:
                return self.respond(400,{'error':'Registro duplicado ou inválido.'})

        def delegate(self,user,method):
            store=control.tenant_store(user['agency_id'])
            parent=self
            class Gate:
                enabled=True
                def hosts(self,port):return parent.hosts()
                def origins(self,port):return {public_url.rstrip('/')} if public_url else {f'http://{h}' for h in parent.hosts()}
                def authenticated(self,cookie):
                    p=control.principal(cookie)
                    return bool(p and p['agency_id']==user['agency_id'] and control.access(p))
            delegate=engine_handler(store,Gate())
            getattr(delegate,'do_'+method)(self)
    return Handler


if __name__=='__main__':
    parser=argparse.ArgumentParser(description='ProspectSTX — administração e acesso de agências')
    parser.add_argument('--init-admin',action='store_true')
    parser.add_argument('--change-admin',action='store_true')
    parser.add_argument('--port',type=int,default=8766)
    args=parser.parse_args();control=Control()
    if args.change_admin:
        import getpass
        try:
            email=input('Novo e-mail do administrador (pode repetir o atual): ')
            password=getpass.getpass('Nova senha (mínimo 12 caracteres): ')
            if password != getpass.getpass('Repita a nova senha: '):
                raise ValueError('As senhas não coincidem.')
            control.change_admin(email,password)
        except ValueError as error:
            raise SystemExit(str(error))
        print('Administrador atualizado. Entre com o novo e-mail e a nova senha.');sys.exit(0)
    if args.init_admin:
        import getpass
        control.bootstrap(input('E-mail do administrador: '),getpass.getpass('Senha (mínimo 12 caracteres): '))
        print('Administrador criado. Execute novamente sem --init-admin.');sys.exit(0)
    host=os.getenv('ORBIT_V2_HOST','127.0.0.1');public=os.getenv('ORBIT_V2_PUBLIC_URL','')
    if host not in ('localhost','127.0.0.1') and not public:
        raise SystemExit('Configure ORBIT_V2_PUBLIC_URL HTTPS antes de acesso externo.')
    with control.db() as db:
        if not db.execute("SELECT 1 FROM users WHERE role='ADMIN'").fetchone():
            raise SystemExit('Crie o administrador primeiro: python server.py --init-admin')
    server=ThreadingHTTPServer((host,args.port),make_handler(control,public))
    print(f'ProspectSTX: http://{host}:{args.port}',flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:server.server_close()
