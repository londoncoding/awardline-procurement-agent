CREATE TABLE IF NOT EXISTS dossiers (
    id text NOT NULL,
    revision text NOT NULL,
    payload jsonb NOT NULL,
    eligible boolean NOT NULL,
    data_as_of timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (id, revision),
    CONSTRAINT dossier_eight_fields CHECK (
        jsonb_typeof(payload) = 'object'
        AND jsonb_array_length(jsonb_path_query_array(payload, '$.*')) = 8
        AND payload ?& ARRAY['dossier','buyer','opportunity','procurement_route','related_awards','material_changes','published_contact','provenance']
    )
);

CREATE TABLE IF NOT EXISTS dossier_heads (
    id text PRIMARY KEY,
    revision text NOT NULL,
    FOREIGN KEY (id, revision) REFERENCES dossiers(id, revision)
);

CREATE TABLE IF NOT EXISTS pilot_customers (
    id uuid PRIMARY KEY,
    token_sha256 char(64) NOT NULL UNIQUE,
    enabled boolean NOT NULL DEFAULT true,
    trial_limit integer NOT NULL DEFAULT 3 CHECK (trial_limit >= 0),
    trial_used integer NOT NULL DEFAULT 0 CHECK (trial_used >= 0 AND trial_used <= trial_limit),
    trial_expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS operations (
    id uuid PRIMARY KEY,
    customer_id uuid NOT NULL REFERENCES pilot_customers(id),
    idempotency_key uuid NOT NULL,
    request_sha256 char(64) NOT NULL,
    dossier_id text NOT NULL,
    revision text NOT NULL,
    funding text NOT NULL CHECK (funding IN ('trial', 'paid')),
    status text NOT NULL CHECK (status IN ('fulfilled', 'pending', 'failed')),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (customer_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS entitlements (
    id uuid PRIMARY KEY,
    customer_id uuid NOT NULL REFERENCES pilot_customers(id),
    dossier_id text NOT NULL,
    revision text NOT NULL,
    grant_type text NOT NULL CHECK (grant_type IN ('trial', 'paid')),
    operation_id uuid NOT NULL REFERENCES operations(id),
    granted_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (customer_id, dossier_id, revision),
    FOREIGN KEY (dossier_id, revision) REFERENCES dossiers(id, revision)
);
CREATE INDEX IF NOT EXISTS operations_customer_status_idx ON operations (customer_id, status, created_at DESC);
