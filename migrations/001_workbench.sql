-- Product 4 hosted workbench persistence. JSON payloads remain canonical.
CREATE TABLE IF NOT EXISTS product4_sessions (
  session_id TEXT PRIMARY KEY,
  revision INTEGER NOT NULL,
  title TEXT NOT NULL,
  frozen_hash TEXT,
  row_generation BIGINT NOT NULL DEFAULT 1,
  payload JSONB NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Keep upgrades from the first local-only hosted draft safe and idempotent.
ALTER TABLE product4_sessions
  ADD COLUMN IF NOT EXISTS frozen_hash TEXT;
ALTER TABLE product4_sessions
  ADD COLUMN IF NOT EXISTS row_generation BIGINT NOT NULL DEFAULT 1;
UPDATE product4_sessions
SET frozen_hash = NULLIF(payload->>'frozen_hash', '')
WHERE frozen_hash IS NULL;

CREATE INDEX IF NOT EXISTS product4_sessions_updated_idx
  ON product4_sessions (updated_at DESC, session_id);

CREATE TABLE IF NOT EXISTS product4_documents (
  session_id TEXT NOT NULL REFERENCES product4_sessions(session_id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('confirmation', 'pipeline', 'glific_result')),
  revision INTEGER NOT NULL,
  frozen_hash TEXT,
  artifact_hash TEXT,
  payload JSONB NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (session_id, kind)
);

CREATE INDEX IF NOT EXISTS product4_documents_binding_idx
  ON product4_documents (session_id, revision, frozen_hash, artifact_hash);

CREATE TABLE IF NOT EXISTS product4_publish_leases (
  session_id TEXT PRIMARY KEY REFERENCES product4_sessions(session_id) ON DELETE CASCADE,
  artifact_hash TEXT NOT NULL,
  owner TEXT NOT NULL,
  lease_until TIMESTAMPTZ NOT NULL,
  acquired_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS product4_publish_leases_expiry_idx
  ON product4_publish_leases (lease_until);
