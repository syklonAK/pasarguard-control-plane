ALTER TABLE accounts ADD COLUMN IF NOT EXISTS balance_irr bigint NOT NULL DEFAULT 0;
INSERT INTO accounts(id,owner_key,code,balance_irr) VALUES ('00000000-0000-0000-0000-000000000001','SYSTEM','wallet',0) ON CONFLICT(owner_key,code) DO NOTHING;
UPDATE accounts a SET balance_irr=COALESCE(x.balance,0) FROM (
 SELECT account_id,SUM(CASE WHEN side='credit' THEN amount_irr ELSE -amount_irr END) balance FROM entries GROUP BY account_id
) x WHERE x.account_id=a.id;
CREATE TABLE IF NOT EXISTS actors(id varchar(36) PRIMARY KEY,telegram_id bigint UNIQUE NOT NULL,display_name varchar(160) NOT NULL DEFAULT '',status varchar(24) NOT NULL DEFAULT 'active');
CREATE INDEX IF NOT EXISTS ix_actor_telegram ON actors(telegram_id);
CREATE TABLE IF NOT EXISTS memberships(id varchar(36) PRIMARY KEY,organization_id varchar(36) NOT NULL REFERENCES organizations(id),actor_id varchar(36) NOT NULL REFERENCES actors(id),role varchar(40) NOT NULL,status varchar(24) NOT NULL DEFAULT 'active',UNIQUE(organization_id,actor_id));
CREATE INDEX IF NOT EXISTS ix_membership_actor ON memberships(actor_id,status);
CREATE TABLE IF NOT EXISTS funding_requests(id varchar(36) PRIMARY KEY,organization_id varchar(36) NOT NULL REFERENCES organizations(id),actor_id varchar(36) NOT NULL REFERENCES actors(id),amount_irr bigint NOT NULL CHECK(amount_irr>0),status varchar(24) NOT NULL DEFAULT 'pending',created_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS ix_funding_status_created ON funding_requests(status,created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_pending_billing_hold ON outbox(topic,aggregate_id) WHERE topic='billing.hold_required' AND status='pending';
