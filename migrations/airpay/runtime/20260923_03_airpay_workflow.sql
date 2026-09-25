-- Долговечные задания и связь переоценки; существующие оплаты не изменяются.
CREATE EXTENSION IF NOT EXISTS pgcrypto;
ALTER TABLE supplier_hub_airpay.airpay_transactions ADD COLUMN IF NOT EXISTS pin_ciphertext bytea;
ALTER TABLE supplier_hub_airpay.airpay_transactions ADD COLUMN IF NOT EXISTS result_hash text NOT NULL DEFAULT '';
CREATE TABLE IF NOT EXISTS supplier_hub_airpay.airpay_jobs (
    id uuid PRIMARY KEY,
    created_by text NOT NULL,
    transaction_id bigint NOT NULL REFERENCES supplier_hub_airpay.airpay_transactions(agent_transaction_id),
    action text NOT NULL CHECK (action IN ('check', 'pay', 'voucher', 'reconcile', 'renew')),
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    state text NOT NULL DEFAULT 'queued' CHECK (state IN ('queued', 'running', 'succeeded', 'failed')),
    progress integer NOT NULL DEFAULT 0,
    result jsonb,
    error text NOT NULL DEFAULT '',
    error_status integer,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE supplier_hub_airpay.airpay_transactions ADD COLUMN IF NOT EXISTS replacement_key uuid;
CREATE TABLE IF NOT EXISTS supplier_hub_airpay.airpay_renewals (
    source_id bigint PRIMARY KEY REFERENCES supplier_hub_airpay.airpay_transactions(agent_transaction_id),
    preparation_key uuid NOT NULL UNIQUE,
    created_by text NOT NULL,
    quantity integer NOT NULL CHECK (quantity BETWEEN 1 AND 20),
    source_transaction_id bigint NOT NULL REFERENCES supplier_hub_airpay.airpay_transactions(agent_transaction_id),
    replacement_id bigint REFERENCES supplier_hub_airpay.airpay_transactions(agent_transaction_id),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS supplier_hub_airpay.airpay_result_access (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    transaction_id bigint NOT NULL REFERENCES supplier_hub_airpay.airpay_transactions(agent_transaction_id),
    viewed_by text NOT NULL,
    viewed_at timestamptz NOT NULL DEFAULT now()
);
