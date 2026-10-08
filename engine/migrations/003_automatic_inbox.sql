ALTER TABLE campaigns ADD COLUMN automation_hash TEXT NOT NULL DEFAULT '';
CREATE TABLE automatic_jobs (
 id INTEGER PRIMARY KEY, provider_id TEXT NOT NULL UNIQUE REFERENCES inbox_events(provider_id) ON DELETE CASCADE,
 recipient_id TEXT NOT NULL REFERENCES instagram_contacts(recipient_id),
 state TEXT NOT NULL DEFAULT 'QUEUED', campaign_id INTEGER REFERENCES campaigns(id) ON DELETE SET NULL,
 created_at TEXT NOT NULL, detail TEXT NOT NULL DEFAULT ''
);
CREATE TABLE automatic_contacts (
 recipient_id TEXT PRIMARY KEY REFERENCES instagram_contacts(recipient_id),
 paused INTEGER NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT ''
);
