-- migrate:no-transaction
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_airpay_events_transaction
ON supplier_hub_airpay.airpay_events(transaction_id, id DESC);
