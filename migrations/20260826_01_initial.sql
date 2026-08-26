CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE SCHEMA IF NOT EXISTS supplier_hub;

CREATE TABLE IF NOT EXISTS supplier_hub.schema_migrations (
    version text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS supplier_hub.purchases (
    id uuid PRIMARY KEY,
    consumer_id text NOT NULL,
    idempotency_key text NOT NULL,
    request_fingerprint text NOT NULL,
    provider_code text NOT NULL,
    service_id bigint NOT NULL CHECK (service_id > 0),
    account text NOT NULL DEFAULT '',
    request_params jsonb NOT NULL DEFAULT '{}'::jsonb,
    provider_operation_id text NOT NULL UNIQUE,
    state text NOT NULL DEFAULT 'created' CHECK (
        state IN ('created', 'checked', 'payment_started', 'processing', 'succeeded', 'failed', 'requires_attention')
    ),
    amount numeric(18, 6),
    provider_status integer,
    provider_message text NOT NULL DEFAULT '',
    provider_transaction_id text NOT NULL DEFAULT '',
    provider_response jsonb NOT NULL DEFAULT '{}'::jsonb,
    result_ciphertext bytea,
    result_hash text NOT NULL DEFAULT '',
    status_check_attempts integer NOT NULL DEFAULT 0 CHECK (status_check_attempts >= 0),
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    lease_token uuid,
    lease_until timestamptz,
    pay_started_at timestamptz,
    completed_at timestamptz,
    last_error text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (consumer_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS supplier_hub_purchases_due_idx
    ON supplier_hub.purchases (next_attempt_at, created_at)
    WHERE state IN ('created', 'checked', 'payment_started', 'processing');

CREATE INDEX IF NOT EXISTS supplier_hub_purchases_provider_transaction_idx
    ON supplier_hub.purchases (provider_transaction_id)
    WHERE provider_transaction_id <> '';

CREATE UNIQUE INDEX IF NOT EXISTS supplier_hub_purchases_result_hash_idx
    ON supplier_hub.purchases (result_hash)
    WHERE result_hash <> '';

CREATE TABLE IF NOT EXISTS supplier_hub.purchase_events (
    id bigserial PRIMARY KEY,
    purchase_id uuid NOT NULL REFERENCES supplier_hub.purchases(id) ON DELETE RESTRICT,
    event_type text NOT NULL,
    from_state text,
    to_state text,
    message text NOT NULL DEFAULT '',
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS supplier_hub_purchase_events_purchase_idx
    ON supplier_hub.purchase_events (purchase_id, id);
