-- Plan catalog: a seller publishes priced packages on a server, and an order can carry one.
-- Append-only: earlier migrations are never edited.

ALTER TABLE panels ADD COLUMN IF NOT EXISTS vendor varchar(32) NOT NULL DEFAULT 'pasarguard';

CREATE TABLE IF NOT EXISTS plan_categories (
  id varchar(36) PRIMARY KEY,
  organization_id varchar(36) NOT NULL REFERENCES organizations(id),
  name varchar(80) NOT NULL,
  description varchar(500) NOT NULL DEFAULT '',
  status varchar(24) NOT NULL DEFAULT 'active',
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_plan_category_name UNIQUE (organization_id, name)
);

CREATE TABLE IF NOT EXISTS plans (
  id varchar(36) PRIMARY KEY,
  organization_id varchar(36) NOT NULL REFERENCES organizations(id),
  panel_id varchar(36) NOT NULL REFERENCES panels(id),
  category_id varchar(36) REFERENCES plan_categories(id),
  name varchar(120) NOT NULL,
  description varchar(1000) NOT NULL DEFAULT '',
  price_per_gib_irr bigint NOT NULL,
  daily_gib integer NOT NULL,
  duration_days integer NOT NULL,
  user_count integer NOT NULL,
  credit_limit_irr bigint NOT NULL DEFAULT 0,
  payment_instructions text NOT NULL DEFAULT '',
  status varchar(24) NOT NULL DEFAULT 'active',
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_plans_panel ON plans(panel_id, status);
CREATE INDEX IF NOT EXISTS ix_plans_organization ON plans(organization_id);

ALTER TABLE panel_orders DROP CONSTRAINT IF EXISTS ck_panel_order_status;
ALTER TABLE panel_orders ADD CONSTRAINT ck_panel_order_status CHECK (
  status IN ('pending','quoted','confirmed','payment_declared','approved','rejected','cancelled'));

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_plan_price') THEN
    ALTER TABLE plans ADD CONSTRAINT ck_plan_price CHECK (
      price_per_gib_irr > 0 AND daily_gib > 0 AND duration_days > 0 AND user_count > 0 AND credit_limit_irr >= 0);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_plan_status') THEN
    ALTER TABLE plans ADD CONSTRAINT ck_plan_status CHECK (status IN ('active','inactive'));
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_plan_category_status') THEN
    ALTER TABLE plan_categories ADD CONSTRAINT ck_plan_category_status CHECK (status IN ('active','inactive'));
  END IF;
END $$;

-- A plan-ordered request is priced by the catalog at creation time, so it needs no manual quote.
ALTER TABLE panel_orders ADD COLUMN IF NOT EXISTS plan_id varchar(36) REFERENCES plans(id);
