-- Отдельный результат TOP_UP; старые покупки остаются ваучерами.
ALTER TABLE supplier_hub.purchases
    ADD COLUMN kind text NOT NULL DEFAULT 'voucher' CHECK (kind IN ('voucher','steam_topup')),
    ADD COLUMN preflight_attempts integer NOT NULL DEFAULT 0,
    ADD COLUMN requested_amount numeric(12,2),
    ADD COLUMN workspace_id bigint,
    ADD COLUMN connection_id bigint,
    ADD CONSTRAINT topup_scope CHECK (kind <> 'steam_topup' OR
      (requested_amount IS NOT NULL AND requested_amount >= 16.99 AND workspace_id IS NOT NULL AND workspace_id > 0 AND connection_id IS NULL
       AND service_id=9361 AND provider_code='interhub' AND account<>''));
CREATE TABLE supplier_hub.topup_permissions (
    consumer_id text NOT NULL,
    workspace_id bigint NOT NULL CHECK (workspace_id > 0),
    enabled boolean NOT NULL DEFAULT false,
    max_amount numeric(12,2) NOT NULL DEFAULT 100 CHECK (max_amount >= 16.99),
    PRIMARY KEY (consumer_id, workspace_id)
);
