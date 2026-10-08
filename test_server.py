import datetime as dt
import json
from pathlib import Path
import uuid
import threading
import unittest
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

from server import Control, COOKIE, make_handler, today


class SaaSTests(unittest.TestCase):
    def setUp(self):
        scratch=Path(__file__).resolve().parents[2]/'work'
        scratch.mkdir(exist_ok=True)
        self.test_root=scratch/('saas-test-'+uuid.uuid4().hex)
        self.control=Control(self.test_root)
        self.control.bootstrap('admin@example.com','AdminPass12345')
        self.http=ThreadingHTTPServer(('127.0.0.1',0),make_handler(self.control))
        self.thread=threading.Thread(target=self.http.serve_forever,daemon=True);self.thread.start()
        self.base=f'http://127.0.0.1:{self.http.server_port}'
        self.admin=self.login('admin@example.com','AdminPass12345')

    def tearDown(self):
        self.http.shutdown();self.http.server_close();self.thread.join()

    def call(self,path,data=None,cookie='',headers=None):
        h={'X-Requested-With':'ProspectLocal'}
        if cookie:h['Cookie']=cookie
        h.update(headers or {})
        req=urllib.request.Request(self.base+path,data=json.dumps(data).encode() if data is not None else None,headers=h)
        try:r=urllib.request.urlopen(req,timeout=10)
        except urllib.error.HTTPError as error:r=error
        body=r.read()
        try:body=json.loads(body)
        except (json.JSONDecodeError,UnicodeDecodeError):pass
        return r.status,body,r.headers

    def login(self,email,password):
        status,body,headers=self.call('/api/auth/login',{'user':email,'password':password})
        self.assertEqual(status,200,body)
        return headers['Set-Cookie'].split(';')[0]

    def agency(self,name,email,status='ACTIVE'):
        expires=(dt.date.fromisoformat(today())+dt.timedelta(days=30)).isoformat()
        code,body,_=self.call('/api/admin/agencies',{'name':name,'email':email,'password':'AgencyPass12345','plan':'ProspectSTX Inicial','price_cents':19700,'status':status,'valid_until':expires},self.admin)
        self.assertEqual(code,200,body)
        return body['result']['id']

    def test_isolation_and_admin_authorization(self):
        a=self.agency('Agência A','a@example.com');b=self.agency('Agência B','b@example.com')
        ca=self.login('a@example.com','AgencyPass12345');cb=self.login('b@example.com','AgencyPass12345')
        code,body,_=self.call('/api/leads',{'company':'Lead privado A','niche':'agencia_marketing'},ca)
        self.assertEqual(code,200,body);lid=body['result']['id']
        code,body,_=self.call('/api/state',cookie=cb);self.assertEqual(code,200);self.assertEqual(body['leads'],[])
        code,body,_=self.call('/api/state',cookie=ca);self.assertEqual(code,200);self.assertEqual(body['leads'][0]['company'],'Lead privado A')
        self.assertEqual(body['config']['agency_name'],'Agência A')
        code,_,_=self.call(f'/api/leads/{lid}/schedule',{'next_action':'Invadir'},cb);self.assertEqual(code,400)
        self.assertEqual(self.call('/api/admin/state',cookie=ca)[0],403)
        self.assertEqual(self.call('/api/admin/access',{'agency_id':b,'enabled':False},ca)[0],403)
        self.assertEqual(self.call('/api/state',cookie=self.admin)[0],404)
        self.assertEqual(self.call('/api/settings',{'agency_name':'Outra'},ca)[0],409)
        self.assertEqual(self.call('/api/prospecting/discover',{},ca)[0],409)
        self.assertEqual(self.control.tenant_store(a).campaigns.adapter.env,{})

    def test_payment_totals_void_and_no_automatic_renewal(self):
        aid=self.agency('Cliente','c@example.com')
        data={'agency_id':aid,'amount_cents':19700,'paid_on':today(),'reference':'Comprovante-1'}
        code,body,_=self.call('/api/admin/payments',data,self.admin);self.assertEqual(code,200)
        pid=body['result']['id']
        self.assertEqual(self.call('/api/admin/payments',data,self.admin)[0],400)
        state=self.control.snapshot();self.assertEqual(state['metrics']['received_cents'],19700)
        self.assertEqual(state['metrics']['paying_agencies'],1)
        self.assertEqual(self.call('/api/admin/payments/void',{'payment_id':pid,'reason':'Duplicado'},self.admin)[0],200)
        self.assertEqual(self.control.snapshot()['metrics']['received_cents'],0)
        self.assertEqual(self.control.snapshot()['metrics']['paying_agencies'],0)
        self.assertEqual(self.control.snapshot()['agencies'][0]['status'],'ACTIVE')
        self.assertTrue(any(x['action']=='PAYMENT_VOIDED' for x in self.control.snapshot()['logs']))

    def test_suspension_expiry_reset_and_trial(self):
        aid=self.agency('Teste','t@example.com','TRIAL');cookie=self.login('t@example.com','AgencyPass12345')
        self.assertEqual(self.call('/api/state',cookie=cookie)[0],200)
        self.assertEqual(self.control.snapshot()['metrics']['active_subscriptions'],0)
        self.assertEqual(self.call('/api/admin/access',{'agency_id':aid,'enabled':False},self.admin)[0],200)
        self.assertEqual(self.call('/api/state',cookie=cookie)[0],401)
        self.assertEqual(self.call('/api/auth/login',{'user':'t@example.com','password':'AgencyPass12345'})[0],403)
        self.call('/api/admin/access',{'agency_id':aid,'enabled':True},self.admin)
        cookie=self.login('t@example.com','AgencyPass12345')
        self.call('/api/admin/password',{'agency_id':aid,'password':'NewAgency12345'},self.admin)
        self.assertEqual(self.call('/api/state',cookie=cookie)[0],401)
        cookie=self.login('t@example.com','NewAgency12345')
        past=(dt.date.fromisoformat(today())-dt.timedelta(days=1)).isoformat()
        self.call('/api/admin/subscription',{'agency_id':aid,'plan':'Inicial','price_cents':19700,'status':'ACTIVE','valid_until':past},self.admin)
        self.assertEqual(self.call('/api/state',cookie=cookie)[0],403)

    def test_no_secret_leaks_and_csrf(self):
        self.agency('Seguro','s@example.com')
        state=json.dumps(self.control.snapshot())
        self.assertNotIn('AgencyPass12345',state);self.assertNotIn('pbkdf2',state)
        self.assertEqual(self.call('/api/admin/access',{'agency_id':1,'enabled':False},self.admin,{'Origin':'https://attacker.example'})[0],403)
        self.assertEqual(self.call('/api/admin/state')[0],401)
        self.assertEqual(self.call('/admin.js',cookie=self.admin)[0],200)
        self.assertEqual(self.call('/api/admin/payments',{'agency_id':1,'amount_cents':1.5,'paid_on':today(),'reference':'bad'},self.admin)[0],400)

    def test_throttle_and_logout(self):
        for _ in range(5):self.assertEqual(self.call('/api/auth/login',{'user':'missing@example.com','password':'WrongPass12345'})[0],401)
        self.assertEqual(self.call('/api/auth/login',{'user':'admin@example.com','password':'AdminPass12345'})[0],429)
        self.assertEqual(self.call('/api/auth/logout',{},self.admin)[0],200)
        self.assertEqual(self.call('/api/admin/state',cookie=self.admin)[0],401)

    def test_change_admin_preserves_clients_and_revokes_session(self):
        aid=self.agency('Preservada','keep@example.com')
        client=self.login('keep@example.com','AgencyPass12345')
        with self.assertRaises(ValueError):
            self.control.change_admin('keep@example.com','NewAdmin12345')
        self.assertEqual(self.call('/api/admin/state',cookie=self.admin)[0],200)
        self.control.change_admin('newadmin@example.com','NewAdmin12345')
        self.assertEqual(self.call('/api/admin/state',cookie=self.admin)[0],401)
        self.assertEqual(self.call('/api/state',cookie=client)[0],200)
        new_admin=self.login('newadmin@example.com','NewAdmin12345')
        self.assertEqual(self.call('/api/admin/state',cookie=new_admin)[0],200)
        self.assertEqual(self.control.snapshot()['agencies'][0]['id'],aid)
        self.assertEqual(self.call('/api/auth/login',{'user':'admin@example.com','password':'AdminPass12345'})[0],401)


if __name__=='__main__':unittest.main()
