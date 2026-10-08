import threading
from contextlib import contextmanager
from app import Store, ROOT
from campaigns import CampaignService
from cloud_db import postgres_db


class CloudStore(Store):
    def __init__(self,agency_id,config):
        self.schema='prospectstx_agency_'+str(agency_id)
        self.config=config
        self.lock=threading.RLock()
        with self.db() as db:
            db.executescript((ROOT/'migrations/001_initial.sql').read_text(encoding='utf-8'))
            db.execute('INSERT OR IGNORE INTO schema_migrations VALUES (?)',('001',))
            for migration in sorted((ROOT/'migrations').glob('*.sql')):
                version=migration.stem.split('_')[0]
                if not db.execute('SELECT 1 FROM schema_migrations WHERE version=?',(version,)).fetchone():
                    db.executescript(migration.read_text(encoding='utf-8'))
                    db.execute('INSERT INTO schema_migrations VALUES (?)',(version,))
        self.campaigns=CampaignService(self)

    @contextmanager
    def db(self):
        with self.lock:
            with postgres_db(self.schema) as db:yield db
