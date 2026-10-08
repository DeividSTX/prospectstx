"""Entrada Gunicorn: o listener HTTP é do servidor de produção."""
import io
import json
import logging
import os
from email.message import Message
from http import HTTPStatus
from types import MethodType, SimpleNamespace
from urllib.parse import urlsplit

from server import Control, make_handler


def build_application(control,public_url):
    handler=make_handler(control,public_url)
    def application(environ,start_response):
        captured=[]
        def respond(self,status,data,mime='application/json; charset=utf-8',headers=None):
            body=data if isinstance(data,bytes) else json.dumps(data,ensure_ascii=False).encode()
            response_headers={'Content-Type':mime,'Content-Length':str(len(body)),
              'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer',
              'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'",**(headers or {})}
            captured.append((status,response_headers,body))
        request=object.__new__(handler)
        request.respond=MethodType(respond,request)
        request.headers=Message()
        for key,value in environ.items():
            if key.startswith('HTTP_'):request.headers[key[5:].replace('_','-')]=value
        if 'CONTENT_LENGTH' in environ:request.headers['Content-Length']=environ['CONTENT_LENGTH']
        if 'CONTENT_TYPE' in environ:request.headers['Content-Type']=environ['CONTENT_TYPE']
        request.path=environ.get('PATH_INFO','/')
        if environ.get('QUERY_STRING'):request.path+='?'+environ['QUERY_STRING']
        request.server=SimpleNamespace(server_port=int(environ.get('SERVER_PORT','443')))
        request.client_address=(environ.get('REMOTE_ADDR','unknown'),0)
        request.rfile=environ['wsgi.input']
        try:
            method=environ.get('REQUEST_METHOD','GET')
            if method not in ('GET','POST'):respond(request,405,{'error':'Método não permitido.'})
            else:getattr(request,'do_'+method)()
        except Exception:
            # Nunca registrar URL do banco, senhas ou payloads em logs.
            logging.error('Falha interna ao processar requisição ProspectSTX.')
            captured=[];respond(request,500,{'error':'Falha interna. Tente novamente.'})
        status,headers,body=captured[-1]
        start_response(f'{status} {HTTPStatus(status).phrase}',list(headers.items()))
        return [body]
    return application


_application=None
def application(environ,start_response):
    global _application
    if _application is None:
        if not os.getenv('DATABASE_URL'):
            raise RuntimeError('Configure DATABASE_URL: a versão Cloud não usa banco temporário.')
        public=os.environ['ORBIT_V2_PUBLIC_URL']
        control=Control()
        with control.db() as db:
            exists=db.execute("SELECT 1 FROM users WHERE role='ADMIN'").fetchone()
        if not exists:
            control.bootstrap(os.environ['ADMIN_EMAIL'],os.environ['ADMIN_PASSWORD'])
        _application=build_application(control,public)
    return _application(environ,start_response)
