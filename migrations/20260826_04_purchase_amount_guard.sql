ALTER TABLE supplier_hub.purchases
    ADD COLUMN IF NOT EXISTS max_amount numeric(18, 6);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'supplier_hub_purchases_max_amount_positive'
          AND conrelid = 'supplier_hub.purchases'::regclass
    ) THEN
        ALTER TABLE supplier_hub.purchases
            ADD CONSTRAINT supplier_hub_purchases_max_amount_positive
            CHECK (max_amount IS NULL OR max_amount > 0) NOT VALID;
        ALTER TABLE supplier_hub.purchases
            VALIDATE CONSTRAINT supplier_hub_purchases_max_amount_positive;
    END IF;
END $$;
