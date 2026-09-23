-- Ledger hardening: balances may only move through double-entry rows, defaults are
-- enforced in the database, and outbox delivery state is tracked per event.

ALTER TABLE transactions ALTER COLUMN reference SET DEFAULT '';
ALTER TABLE transactions ALTER COLUMN kind SET DEFAULT 'entry';
ALTER TABLE transactions ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS actor_id varchar(36);
ALTER TABLE entries ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE accounts ALTER COLUMN balance_irr SET DEFAULT 0;
ALTER TABLE accounts ALTER COLUMN code SET DEFAULT 'wallet';
ALTER TABLE memberships ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE contracts ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE panels ADD COLUMN IF NOT EXISTS usage_coefficient numeric(10,4) NOT NULL DEFAULT 1.0000;
ALTER TABLE panels ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE bindings ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE actors ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE funding_requests ADD COLUMN IF NOT EXISTS decided_by varchar(36);
ALTER TABLE funding_requests ADD COLUMN IF NOT EXISTS decided_at timestamptz;
ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS request_id varchar(64);
ALTER TABLE approval_requests ADD COLUMN IF NOT EXISTS decision_note text;
ALTER TABLE outbox ADD COLUMN IF NOT EXISTS attempts integer NOT NULL DEFAULT 0;
ALTER TABLE outbox ADD COLUMN IF NOT EXISTS next_attempt_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE outbox ADD COLUMN IF NOT EXISTS claimed_by varchar(80);
ALTER TABLE outbox ADD COLUMN IF NOT EXISTS claimed_at timestamptz;
ALTER TABLE outbox ADD COLUMN IF NOT EXISTS completed_at timestamptz;
ALTER TABLE outbox ADD COLUMN IF NOT EXISTS last_error text;
ALTER TABLE outbox ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now();

CREATE INDEX IF NOT EXISTS ix_closure_ancestor ON organization_closure(ancestor_id);
CREATE INDEX IF NOT EXISTS ix_org_parent ON organizations(parent_id);
CREATE INDEX IF NOT EXISTS ix_binding_panel ON bindings(panel_id);
CREATE INDEX IF NOT EXISTS ix_outbox_claim ON outbox(status,next_attempt_at);
CREATE INDEX IF NOT EXISTS ix_transaction_actor ON transactions(actor_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_outbox_pending_per_aggregate ON outbox(topic,aggregate_id) WHERE status='pending';

DO $$ BEGIN
  ALTER TABLE entries DROP CONSTRAINT IF EXISTS ck_entry_positive;
  ALTER TABLE entries ADD CONSTRAINT ck_entry_positive CHECK (amount_irr > 0);
END $$;

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_org_status') THEN
    ALTER TABLE organizations ADD CONSTRAINT ck_org_status CHECK (status IN ('active','suspended','closed'));
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_binding_status') THEN
    ALTER TABLE bindings ADD CONSTRAINT ck_binding_status CHECK (status IN ('active','suspended','disabled'));
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_panel_status') THEN
    ALTER TABLE panels ADD CONSTRAINT ck_panel_status CHECK (status IN ('active','disabled'));
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_membership_role') THEN
    ALTER TABLE memberships ADD CONSTRAINT ck_membership_role CHECK (role IN ('system_admin','reseller_admin','operator','finance','support','viewer'));
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_panel_coefficient') THEN
    ALTER TABLE panels ADD CONSTRAINT ck_panel_coefficient CHECK (usage_coefficient > 0 AND usage_coefficient <= 1000);
  END IF;
END $$;

CREATE OR REPLACE FUNCTION guard_account_balance() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.balance_irr IS DISTINCT FROM OLD.balance_irr
     AND coalesce(current_setting('control.ledger_write', true), '') <> 'on' THEN
    RAISE EXCEPTION 'account balance may only change through a ledger transaction';
  END IF;
  NEW.updated_at := now();
  RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_guard_account_balance ON accounts;
CREATE TRIGGER trg_guard_account_balance BEFORE UPDATE ON accounts FOR EACH ROW EXECUTE FUNCTION guard_account_balance();

CREATE OR REPLACE VIEW ledger_imbalance AS
SELECT a.id AS account_id, a.owner_key, a.code, a.balance_irr,
       COALESCE(x.signed, 0) AS entries_balance,
       a.balance_irr - COALESCE(x.signed, 0) AS drift
FROM accounts a
LEFT JOIN (
  SELECT account_id, SUM(CASE WHEN side='credit' THEN amount_irr ELSE -amount_irr END) AS signed
  FROM entries GROUP BY account_id
) x ON x.account_id = a.id
WHERE a.balance_irr IS DISTINCT FROM COALESCE(x.signed, 0);
