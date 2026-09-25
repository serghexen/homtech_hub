-- migrate:no-transaction
-- Одно активное действие на покупку; очередь восстанавливается после перезапуска.
CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_airpay_jobs_active
    ON supplier_hub_airpay.airpay_jobs(created_by, transaction_id) WHERE state IN ('queued', 'running');
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_airpay_jobs_pending
    ON supplier_hub_airpay.airpay_jobs(created_at) WHERE state IN ('queued', 'running');
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_airpay_result_access_transaction
    ON supplier_hub_airpay.airpay_result_access(transaction_id, viewed_at DESC);
CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_airpay_result_hash
    ON supplier_hub_airpay.airpay_transactions(result_hash) WHERE result_hash <> '';
