-- Customer funnel: a Telegram account orders a panel, an admin quotes and approves it.
-- Append-only: earlier migrations are never edited.

ALTER TABLE panels ADD COLUMN IF NOT EXISTS saleable boolean NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS panel_orders (
  id varchar(36) PRIMARY KEY,
  actor_id varchar(36) NOT NULL REFERENCES actors(id),
  panel_id varchar(36) NOT NULL REFERENCES panels(id),
  business_name varchar(160) NOT NULL,
  slug varchar(80) NOT NULL UNIQUE,
  user_count integer NOT NULL,
  daily_gib integer NOT NULL,
  note varchar(500) NOT NULL DEFAULT '',
  status varchar(24) NOT NULL DEFAULT 'pending',
  credit_limit_irr bigint NOT NULL DEFAULT 0,
  price_per_gib_irr bigint NOT NULL DEFAULT 0,
  amount_irr bigint NOT NULL DEFAULT 0,
  payment_instructions text NOT NULL DEFAULT '',
  admin_username varchar(80) NOT NULL DEFAULT '',
  pg_admin_id integer,
  organization_id varchar(36) REFERENCES organizations(id),
  decided_by varchar(36),
  decided_at timestamptz,
  rejection_reason varchar(500) NOT NULL DEFAULT '',
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_panel_orders_queue ON panel_orders(status,created_at);
CREATE INDEX IF NOT EXISTS ix_panel_orders_actor ON panel_orders(actor_id);

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_panel_order_status') THEN
    ALTER TABLE panel_orders ADD CONSTRAINT ck_panel_order_status CHECK (
      status IN ('pending','quoted','confirmed','payment_declared','approved','rejected','cancelled'));
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_panel_order_size') THEN
    ALTER TABLE panel_orders ADD CONSTRAINT ck_panel_order_size CHECK (user_count > 0 AND daily_gib > 0);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_panel_order_quote') THEN
    ALTER TABLE panel_orders ADD CONSTRAINT ck_panel_order_quote CHECK (credit_limit_irr >= 0 AND price_per_gib_irr >= 0);
  END IF;
  -- The customer role is granted by approving an order, so the membership check must accept it.
  IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_membership_role') THEN
    ALTER TABLE memberships DROP CONSTRAINT ck_membership_role;
  END IF;
  ALTER TABLE memberships ADD CONSTRAINT ck_membership_role CHECK (
    role IN ('system_admin','reseller_admin','operator','finance','support','viewer','customer'));
END $$;
