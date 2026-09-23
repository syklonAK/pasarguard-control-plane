-- Delivery state for queued events and server-side defaults for approval requests.
-- Append-only: earlier migrations are never edited.

ALTER TABLE outbox ALTER COLUMN status SET DEFAULT 'pending';
ALTER TABLE outbox ALTER COLUMN attempts SET DEFAULT 0;
ALTER TABLE approval_requests ALTER COLUMN status SET DEFAULT 'pending';
ALTER TABLE approval_requests ALTER COLUMN payload SET DEFAULT '{}';
ALTER TABLE approval_requests ALTER COLUMN payload SET NOT NULL;
ALTER TABLE approval_requests ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE audit_logs ALTER COLUMN metadata SET DEFAULT '{}';

CREATE INDEX IF NOT EXISTS ix_outbox_retry ON outbox(status,next_attempt_at,created_at);
CREATE INDEX IF NOT EXISTS ix_approval_status ON approval_requests(status,expires_at);
CREATE INDEX IF NOT EXISTS ix_audit_action ON audit_logs(action,created_at);

DO $do$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_outbox_status') THEN
    ALTER TABLE outbox ADD CONSTRAINT ck_outbox_status CHECK (status IN ('pending','claimed','done','dead'));
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_outbox_attempts') THEN
    ALTER TABLE outbox ADD CONSTRAINT ck_outbox_attempts CHECK (attempts >= 0);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_approval_status') THEN
    ALTER TABLE approval_requests ADD CONSTRAINT ck_approval_status CHECK (status IN ('pending','approved','rejected','expired','executed'));
  END IF;
END $do$;
