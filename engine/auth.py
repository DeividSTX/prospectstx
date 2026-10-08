"""Login de um administrador; sessões opacas e limites persistentes de tentativas."""
import hashlib
import hmac
import os
import secrets
import time
from http.cookies import SimpleCookie
from urllib.parse import urlsplit

COOKIE = 'orbit_session'
TTL = 12 * 60 * 60


def password_hash(password):
    if not isinstance(password, str) or len(password) < 12:
        raise ValueError('Use uma senha com pelo menos 12 caracteres.')
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 600_000).hex()
    return f'pbkdf2_sha256$600000${salt}${digest}'


class Auth:
    def __init__(self, store, env=None):
        env = os.environ if env is None else env
        self.store = store
        self.enabled = env.get('ORBIT_AUTH_ENABLED', 'false').lower() == 'true'
        self.user = env.get('ORBIT_AUTH_USER', '').strip().casefold()
        self.digest = env.get('ORBIT_AUTH_PASSWORD_HASH', '')
        self.public_url = env.get('PUBLIC_URL', '').rstrip('/')
        self.backend_url = env.get('BACKEND_URL', '').rstrip('/')
        for url in (self.public_url, self.backend_url):
            parsed = urlsplit(url)
            if url and (parsed.scheme != 'https' or not parsed.hostname or parsed.path not in ('', '/') or parsed.query or parsed.fragment or parsed.username):
                raise ValueError('URLs públicas devem usar HTTPS e não conter caminho ou credenciais.')
        if self.public_url and not self.enabled:
            raise ValueError('Ative o login antes de publicar o Orbit.')
        if self.enabled:
            try:
                algorithm, rounds, salt, digest = self.digest.split('$')
                valid = algorithm == 'pbkdf2_sha256' and 600_000 <= int(rounds) <= 1_000_000 and len(bytes.fromhex(salt)) == 16 and len(bytes.fromhex(digest)) == 32
            except (ValueError, TypeError):
                valid = False
            if not valid or not self.user:
                raise ValueError('Configure o usuário e o hash da senha do Orbit.')

    def hosts(self, port):
        if self.public_url:
            return {urlsplit(u).netloc for u in (self.public_url, self.backend_url) if u}
        return {f'127.0.0.1:{port}', f'localhost:{port}'}

    def origins(self, port):
        if self.public_url:
            return {u for u in (self.public_url, self.backend_url) if u}
        return {f'http://127.0.0.1:{port}', f'http://localhost:{port}'}

    def token(self, cookie):
        try:
            jar = SimpleCookie()
            jar.load(cookie or '')
            value = jar[COOKIE].value if COOKIE in jar else ''
            return value if len(value) == 43 else ''
        except Exception:
            return ''

    def authenticated(self, cookie):
        if not self.enabled:
            return True
        token = self.token(cookie)
        if not token:
            return False
        with self.store.db() as db:
            return bool(db.execute('SELECT 1 FROM auth_sessions WHERE digest=? AND expires_at>?', (hashlib.sha256(token.encode()).hexdigest(), int(time.time()))).fetchone())

    def login(self, user, password, address):
        if not self.enabled:
            return '', 400
        if not isinstance(user, str) or not isinstance(password, str) or len(user) > 254 or len(password) > 1024:
            return '', 401
        current = int(time.time())
        # Never trust a caller-supplied X-Forwarded-For to evade the throttle.
        bucket = hashlib.sha256(address.encode()).hexdigest()
        with self.store.db() as db:
            db.execute('DELETE FROM auth_attempts WHERE attempted_at<?', (current - 900,))
            if db.execute('SELECT count(*) FROM auth_attempts WHERE bucket=?', (bucket,)).fetchone()[0] >= 5:
                return '', 429
            db.execute('INSERT INTO auth_attempts(bucket,attempted_at) VALUES (?,?)', (bucket, current))
        _, rounds, salt, expected = self.digest.split('$')
        actual = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), int(rounds)).hex()
        matches = hmac.compare_digest(actual, expected)
        if not hmac.compare_digest(user.strip().casefold().encode(), self.user.encode()) or not matches:
            return '', 401
        token = secrets.token_urlsafe(32)
        with self.store.db() as db:
            db.execute('DELETE FROM auth_attempts WHERE bucket=?', (bucket,))
            db.execute('DELETE FROM auth_sessions WHERE expires_at<=?', (current,))
            db.execute('INSERT INTO auth_sessions(digest,expires_at) VALUES (?,?)', (hashlib.sha256(token.encode()).hexdigest(), current+TTL))
            self.store.log(db, None, 'LOGIN', 'Administrador autenticado.')
        return token, 200

    def logout(self, cookie):
        token = self.token(cookie)
        with self.store.db() as db:
            db.execute('DELETE FROM auth_sessions WHERE digest=?', (hashlib.sha256(token.encode()).hexdigest(),))

    def cookie(self, token=''):
        return f'{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={TTL if token else 0}' + ('; Secure' if self.public_url else '')
