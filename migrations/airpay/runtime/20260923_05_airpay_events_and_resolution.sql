-- История Airpay и ручные решения; старые операции не переписываются.
ALTER TABLE supplier_hub_airpay.airpay_transactions ADD COLUMN IF NOT EXISTS requires_attention boolean NOT NULL DEFAULT false;
ALTER TABLE supplier_hub_airpay.airpay_transactions ADD COLUMN IF NOT EXISTS attention_reason text NOT NULL DEFAULT '';
ALTER TABLE supplier_hub_airpay.airpay_transactions ADD COLUMN IF NOT EXISTS resolution_request_id uuid;

CREATE TABLE IF NOT EXISTS supplier_hub_airpay.airpay_operator_actions (
    transaction_id bigint NOT NULL REFERENCES supplier_hub_airpay.airpay_transactions(agent_transaction_id),
    request_id uuid NOT NULL,
    request_hash text NOT NULL,
    decision text NOT NULL CHECK (decision IN ('confirm_failed','record_success')),
    evidence text NOT NULL,
    decided_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (transaction_id, request_id)
);

CREATE TABLE IF NOT EXISTS supplier_hub_airpay.airpay_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    transaction_id bigint NOT NULL REFERENCES supplier_hub_airpay.airpay_transactions(agent_transaction_id),
    event_type text NOT NULL,
    state_before text,
    state_after text NOT NULL,
    actor text NOT NULL,
    details jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- Событие коммитится вместе с изменением, включая изменения из worker и переоценку.
CREATE OR REPLACE FUNCTION supplier_hub_airpay.airpay_record_event() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE event_name text;
BEGIN
    IF TG_OP = 'INSERT' THEN event_name := 'prepared';
    ELSIF NEW.resolution_request_id IS DISTINCT FROM OLD.resolution_request_id THEN event_name := 'operator_resolved';
    ELSIF NEW.requires_attention AND NOT OLD.requires_attention THEN event_name := 'attention_required';
    ELSIF NEW.replacement_key IS DISTINCT FROM OLD.replacement_key THEN event_name := 'replaced';
    ELSIF NEW.pay_attempts > OLD.pay_attempts THEN event_name := 'pay_requested';
    ELSIF NEW.voucher_attempts > OLD.voucher_attempts THEN event_name := 'voucher_requested';
    ELSIF NEW.check_attempts > OLD.check_attempts THEN event_name := 'check_requested';
    ELSIF NEW.result_hash IS DISTINCT FROM OLD.result_hash THEN event_name := 'voucher_saved';
    ELSIF NEW.state IS DISTINCT FROM OLD.state THEN event_name := 'state_changed';
    ELSE event_name := 'operation_updated'; END IF;
    INSERT INTO supplier_hub_airpay.airpay_events(transaction_id,event_type,state_before,state_after,actor,details)
    VALUES(NEW.agent_transaction_id,event_name,CASE WHEN TG_OP='UPDATE' THEN OLD.state ELSE NULL END,
        NEW.state,NEW.created_by,jsonb_build_object(
            'amount',NEW.amount::text,'currency',NEW.currency,
            'provider_transaction_id',NEW.provider_transaction_id,
            'check_attempts',NEW.check_attempts,'pay_attempts',NEW.pay_attempts,
            'voucher_attempts',NEW.voucher_attempts,'requires_attention',NEW.requires_attention,
            'attention_reason',NEW.attention_reason,'resolution_request_id',NEW.resolution_request_id,
            'result_available',NEW.pin_ciphertext IS NOT NULL OR NEW.pin_code<>''));
    RETURN NEW;
END;
$$;
CREATE TRIGGER airpay_transaction_event AFTER INSERT OR UPDATE ON supplier_hub_airpay.airpay_transactions
FOR EACH ROW EXECUTE FUNCTION supplier_hub_airpay.airpay_record_event();
