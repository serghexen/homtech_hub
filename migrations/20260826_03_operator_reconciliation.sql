CREATE TABLE IF NOT EXISTS supplier_hub.operator_actions (
    id uuid PRIMARY KEY,
    purchase_id uuid NOT NULL REFERENCES supplier_hub.purchases(id) ON DELETE RESTRICT,
    operator_id text NOT NULL,
    request_id text NOT NULL,
    action_fingerprint text NOT NULL,
    decision text NOT NULL CHECK (decision IN ('confirm_failed', 'record_success')),
    reason text NOT NULL DEFAULT '',
    result_hash text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (operator_id, request_id)
);

CREATE INDEX IF NOT EXISTS supplier_hub_operator_actions_purchase_idx
    ON supplier_hub.operator_actions (purchase_id, created_at);

CREATE INDEX IF NOT EXISTS supplier_hub_purchases_attention_idx
    ON supplier_hub.purchases (created_at, id)
    WHERE state='requires_attention';
