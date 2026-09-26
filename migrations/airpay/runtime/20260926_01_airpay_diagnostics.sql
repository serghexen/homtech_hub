-- Отдельный журнал диагностических check, без платёжных операций и токенов покупки.
CREATE TABLE IF NOT EXISTS supplier_hub_airpay.airpay_diagnostic_runs (
    id uuid PRIMARY KEY,
    created_by text NOT NULL,
    state text NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'completed', 'cancelled')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS airpay_diagnostic_one_active
    ON supplier_hub_airpay.airpay_diagnostic_runs ((state)) WHERE state = 'active';
CREATE TABLE IF NOT EXISTS supplier_hub_airpay.airpay_diagnostic_items (
    run_id uuid NOT NULL REFERENCES supplier_hub_airpay.airpay_diagnostic_runs(id),
    position integer NOT NULL,
    service_id text NOT NULL,
    title text NOT NULL,
    state text NOT NULL DEFAULT 'pending',
    agent_transaction_id bigint UNIQUE,
    started_at timestamptz,
    finished_at timestamptz,
    report jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (run_id, position)
);
