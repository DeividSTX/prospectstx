"""Entrada isolada para túnel HTTPS: nunca encaminha painel ou API do CRM."""
import http.client
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

WEBHOOK_PATH = '/webhooks/instagram'


def relay_handler(upstream_port=8765):
    class Relay(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def log_message(self, *args):
            # URLs de verificação podem conter token: nunca registrar requests brutos.
            pass

        def answer(self, status, body=b'', content_type='text/plain; charset=utf-8'):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(body)

        def forward(self, method):
            if urlsplit(self.path).path != WEBHOOK_PATH:
                return self.answer(404, b'Not found')
            if len(self.path) > 8192:
                return self.answer(414)
            body = None
            headers = {}
            if method == 'POST':
                lengths = self.headers.get_all('Content-Length', [])
                if len(lengths) != 1 or self.headers.get('Transfer-Encoding'):
                    return self.answer(411)
                try:
                    length = int(lengths[0])
                except ValueError:
                    return self.answer(400)
                if not 0 < length <= 1_000_000:
                    return self.answer(413)
                signature = self.headers.get('X-Hub-Signature-256', '')
                if not re.fullmatch(r'sha256=[a-fA-F0-9]{64}', signature):
                    return self.answer(403)
                body = self.rfile.read(length)
                if len(body) != length:
                    return self.answer(400)
                headers = {'X-Hub-Signature-256': signature, 'Content-Type': 'application/json'}
            connection = http.client.HTTPConnection('127.0.0.1', upstream_port, timeout=25)
            try:
                connection.request(method, self.path, body=body, headers=headers)
                response = connection.getresponse()
                content = response.read(1_000_001)
                if len(content) > 1_000_000:
                    return self.answer(502, b'Upstream response exceeds limit')
                return self.answer(response.status, content, response.getheader('Content-Type', 'text/plain'))
            except (OSError, http.client.HTTPException):
                return self.answer(502, b'Local webhook unavailable')
            finally:
                connection.close()

        def do_GET(self):
            self.forward('GET')

        def do_POST(self):
            self.forward('POST')

    return Relay


if __name__ == '__main__':
    upstream = int(os.getenv('PORT', '8765'))
    port = int(os.getenv('WEBHOOK_RELAY_PORT', '8767'))
    server = ThreadingHTTPServer(('127.0.0.1', port), relay_handler(upstream))
    print(f'Entrada isolada: http://127.0.0.1:{port}{WEBHOOK_PATH}', flush=True)
    print('Painel e API do CRM bloqueados nesta entrada.', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()
