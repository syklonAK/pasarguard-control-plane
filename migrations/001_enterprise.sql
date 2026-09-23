CREATE INDEX IF NOT EXISTS ix_closure_descendant_depth ON organization_closure(descendant_id,depth);
CREATE INDEX IF NOT EXISTS ix_contract_parent_status ON contracts(parent_id,status);
CREATE INDEX IF NOT EXISTS ix_usage_binding_observed ON usage_observations(binding_id,observed_at DESC);
CREATE INDEX IF NOT EXISTS ix_outbox_status_topic ON outbox(status,topic);
CREATE INDEX IF NOT EXISTS ix_transactions_created ON transactions(created_at DESC);
CREATE INDEX IF NOT EXISTS ix_entries_account_tx ON entries(account_id,transaction_id);
DO $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_entry_positive') THEN
  ALTER TABLE entries ADD CONSTRAINT ck_entry_positive CHECK (amount_irr >= 0);
 END IF;
 IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_entry_side') THEN
  ALTER TABLE entries ADD CONSTRAINT ck_entry_side CHECK (side IN ('debit','credit'));
 END IF;
END $$;

CREATE OR REPLACE FUNCTION forbid_ledger_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'ledger is append-only'; END $$;
DROP TRIGGER IF EXISTS trg_no_transaction_update ON transactions;
CREATE TRIGGER trg_no_transaction_update BEFORE UPDATE OR DELETE ON transactions FOR EACH ROW EXECUTE FUNCTION forbid_ledger_mutation();
DROP TRIGGER IF EXISTS trg_no_entry_update ON entries;
CREATE TRIGGER trg_no_entry_update BEFORE UPDATE OR DELETE ON entries FOR EACH ROW EXECUTE FUNCTION forbid_ledger_mutation();

CREATE TABLE IF NOT EXISTS audit_logs(
 id uuid PRIMARY KEY, actor_id varchar(36), organization_id varchar(36), action varchar(100) NOT NULL,
 target_type varchar(80) NOT NULL, target_id varchar(100) NOT NULL, metadata jsonb NOT NULL DEFAULT '{}', created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_audit_org_created ON audit_logs(organization_id,created_at DESC);

CREATE TABLE IF NOT EXISTS approval_requests(
 id uuid PRIMARY KEY, requester_id varchar(36) NOT NULL, approver_id varchar(36), action varchar(100) NOT NULL,
 target_type varchar(80) NOT NULL, target_id varchar(100) NOT NULL, payload jsonb NOT NULL,
 status varchar(24) NOT NULL DEFAULT 'pending', expires_at timestamptz NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), decided_at timestamptz
);
CREATE INDEX IF NOT EXISTS ix_approval_pending ON approval_requests(status,expires_at) WHERE status='pending';
