CREATE TABLE IF NOT EXISTS supplier_hub.result_access_events (
    id uuid PRIMARY KEY,
    purchase_id uuid NOT NULL REFERENCES supplier_hub.purchases(id) ON DELETE RESTRICT,
    operator_id text NOT NULL,
    request_id text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (operator_id, request_id)
);

CREATE INDEX IF NOT EXISTS supplier_hub_result_access_purchase_idx
    ON supplier_hub.result_access_events (purchase_id, created_at);

CREATE INDEX IF NOT EXISTS supplier_hub_purchases_history_idx
    ON supplier_hub.purchases (created_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS supplier_hub_purchases_history_state_idx
    ON supplier_hub.purchases (state, created_at DESC, id DESC);
