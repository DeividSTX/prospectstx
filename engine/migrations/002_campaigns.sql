CREATE TABLE IF NOT EXISTS instagram_contacts (
 recipient_id TEXT PRIMARY KEY, lead_id INTEGER REFERENCES leads(id) ON DELETE SET NULL,
 last_inbound_at TEXT NOT NULL, username TEXT NOT NULL DEFAULT '',
 suppressed INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS instagram_contact_lead ON instagram_contacts(lead_id) WHERE lead_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS inbox_events (
 id INTEGER PRIMARY KEY, provider_id TEXT UNIQUE NOT NULL,
 recipient_id TEXT NOT NULL REFERENCES instagram_contacts(recipient_id),
 body TEXT NOT NULL, occurred_at TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS campaigns (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'DRAFT',
 daily_limit INTEGER NOT NULL, interval_seconds INTEGER NOT NULL,
 created_at TEXT NOT NULL, authorized_at TEXT, expires_at TEXT,
 last_dispatch_at TEXT, note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS campaign_items (
 id INTEGER PRIMARY KEY, campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
 lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
 message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
 recipient_id TEXT NOT NULL REFERENCES instagram_contacts(recipient_id),
 body TEXT NOT NULL, evidence_hash TEXT NOT NULL, review TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'READY', error TEXT NOT NULL DEFAULT '',
 UNIQUE(campaign_id,lead_id)
);
CREATE TABLE IF NOT EXISTS deliveries (
 id INTEGER PRIMARY KEY, item_id INTEGER UNIQUE NOT NULL REFERENCES campaign_items(id) ON DELETE CASCADE,
 state TEXT NOT NULL, provider_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
