"""Версионированный контракт Airpay между CRM и единственным исполнителем."""

CONTRACT_VERSION = 'airpay.v1'
CONTRACT_HEADER = 'X-Airpay-Contract'


def describe_contract(backend, payments_enabled):
    # Возможности описывают именно Airpay и не меняют API действующего Interhub.
    return {
        'contract_version': CONTRACT_VERSION, 'provider_code': 'airpay',
        'execution_backend': backend, 'payments_enabled': bool(payments_enabled),
        'service_id_type': 'string', 'transaction_id_type': 'string',
        'money_type': 'decimal_string', 'max_voucher_quantity': 20,
        'purchase_kinds': ['voucher', 'topup'], 'stock_quantity_available': False,
        'reconcile_uses_pay': True, 'automatic_pay_retry': False,
        'result_reveal_requires_action': True,
        'operator_resolution': True, 'event_history': True,
        'queue_diagnostics': True, 'queued_job_cancellation': True,
        'interrupted_payment_job_replay': False,
    }


def transaction_outcome(row):
    # Оплаченный ваучер ещё не готов к выдаче; пополнение не ожидает секретного кода.
    state = row['state']
    paid = state == 'paid'
    voucher = row['purchase_kind'] == 'voucher'
    available = paid and voucher and bool(row.get('pin_ciphertext') or row.get('pin_code'))
    payment_state = state if state in {'processing', 'paid', 'failed'} else 'not_started'
    result_state = ('ready' if available else 'pending' if paid else 'unavailable') if voucher else 'not_required'
    return {
        'contract_version': CONTRACT_VERSION, 'provider_code': 'airpay',
        'payment_state': payment_state, 'result_state': result_state,
        'result_available': available, 'completed': paid and (available or not voucher),
        'blocks_fallback': state != 'failed',
        'requires_attention': bool(row.get('requires_attention')),
        'attention_reason': row.get('attention_reason', ''),
        'resolution_available': state == 'processing' or (paid and voucher and not available),
    }
