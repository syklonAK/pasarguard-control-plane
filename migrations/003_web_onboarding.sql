ALTER TABLE panels ALTER COLUMN api_key_ref TYPE text;
ALTER TABLE panels ALTER COLUMN owner_user_ref TYPE text;
ALTER TABLE panels ALTER COLUMN owner_pass_ref TYPE text;

CREATE TABLE IF NOT EXISTS panel_owners(
  panel_id varchar(36) PRIMARY KEY REFERENCES panels(id),
  organization_id varchar(36) NOT NULL REFERENCES organizations(id)
);
CREATE INDEX IF NOT EXISTS ix_panel_owners_organization ON panel_owners(organization_id);

CREATE TABLE IF NOT EXISTS system_settings(
  key varchar(100) PRIMARY KEY,
  value text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
