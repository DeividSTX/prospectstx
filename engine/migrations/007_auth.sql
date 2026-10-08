CREATE TABLE auth_sessions (digest TEXT PRIMARY KEY, expires_at INTEGER NOT NULL);
CREATE TABLE auth_attempts (id INTEGER PRIMARY KEY, bucket TEXT NOT NULL, attempted_at INTEGER NOT NULL);
CREATE INDEX auth_attempts_bucket ON auth_attempts(bucket, attempted_at);
