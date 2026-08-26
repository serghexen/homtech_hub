ALTER TABLE supplier_hub.purchases
    ADD COLUMN IF NOT EXISTS request_id text NOT NULL DEFAULT '';

UPDATE supplier_hub.purchases
SET request_id = id::text
WHERE request_id = '';

ALTER TABLE supplier_hub.purchases
    ALTER COLUMN request_id SET DEFAULT gen_random_uuid()::text;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'supplier_hub_purchases_request_id_length'
          AND conrelid = 'supplier_hub.purchases'::regclass
    ) THEN
        ALTER TABLE supplier_hub.purchases
            ADD CONSTRAINT supplier_hub_purchases_request_id_length
            CHECK (char_length(request_id) BETWEEN 1 AND 128) NOT VALID;
        ALTER TABLE supplier_hub.purchases
            VALIDATE CONSTRAINT supplier_hub_purchases_request_id_length;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS supplier_hub_purchases_consumer_request_idx
    ON supplier_hub.purchases (consumer_id, request_id);

ALTER TABLE supplier_hub.purchase_events
    ADD COLUMN IF NOT EXISTS request_id text NOT NULL DEFAULT '';

UPDATE supplier_hub.purchase_events AS event
SET request_id = purchase.request_id
FROM supplier_hub.purchases AS purchase
WHERE event.purchase_id = purchase.id
  AND event.request_id = '';

CREATE INDEX IF NOT EXISTS supplier_hub_purchase_events_request_idx
    ON supplier_hub.purchase_events (request_id, id);
