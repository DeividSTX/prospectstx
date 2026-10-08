CREATE TABLE prospect_candidates (
 id INTEGER PRIMARY KEY, profile_url TEXT UNIQUE NOT NULL, username TEXT NOT NULL,
 company TEXT NOT NULL, niche TEXT NOT NULL, city TEXT NOT NULL,
 snippet TEXT NOT NULL, source TEXT NOT NULL, created_at TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'RESEARCH', service TEXT NOT NULL DEFAULT '',
 observation TEXT NOT NULL DEFAULT '', evidence_url TEXT NOT NULL DEFAULT '',
 approach TEXT NOT NULL DEFAULT ''
);
CREATE TABLE prospect_search_runs (
 id INTEGER PRIMARY KEY, day TEXT NOT NULL, niche TEXT NOT NULL, city TEXT NOT NULL,
 state TEXT NOT NULL, created_at TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
 UNIQUE(day,niche,city)
);
