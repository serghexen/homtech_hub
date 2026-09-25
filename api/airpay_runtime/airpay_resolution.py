"""Ручное решение не получает транспорт поставщика и никогда не вызывает pay."""
from datetime import datetime
from hashlib import sha256
import json

from fastapi import HTTPException
from airpay_runtime.airpay_preparation import amount_value


def resolution_fingerprint(payload):
    # Ключ повтора привязан ко всему решению; открытый код не сохраняется в аудите.
    return sha256(json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str).encode()).hexdigest()


def resolution_changes(row, payload):
    # Решение принимается только по свежей незавершённой операции после внешней сверки владельцем.
    stamp = payload['expected_updated_at']
    if isinstance(stamp, str):
        stamp = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
    if stamp != row['updated_at']:
        raise HTTPException(409, 'Операция изменилась. Обновите результат перед решением.')
    evidence = payload['evidence'].strip()
    if payload['verified'] is not True or not 10 <= len(evidence) <= 1000:
        raise HTTPException(422, 'Подтвердите внешнюю сверку и укажите её основание (10–1000 символов).')
    pending = row['state'] == 'processing'
    missing = row['state'] == 'paid' and row['purchase_kind'] == 'voucher' and not row.get('pin_code')
    if not pending and not missing:
        raise HTTPException(409, 'Ручное решение доступно только для неизвестной оплаты или оплаченного ваучера без кода.')
    code = payload.get('code', '').strip()
    provider_id = payload.get('provider_transaction_id', '').strip()
    changes = dict(requires_attention=False, attention_reason='', next_attempt_at=None,
                   resolution_request_id=payload['request_id'])
    if payload['decision'] == 'confirm_failed':
        if not pending or code or provider_id:
            raise HTTPException(409, 'Подтверждённую оплату нельзя отменять; для отказа не передавайте код и номер оплаты.')
        return {**changes, 'state': 'failed', 'provider_message': 'Владелец подтвердил отсутствие оплаты по результатам внешней сверки.'}
    if payload['decision'] != 'record_success' or not provider_id or len(provider_id) > 128:
        raise HTTPException(422, 'Для успешного решения нужен номер подтверждённой оплаты Airpay.')
    if row['state'] == 'paid' and provider_id != row['provider_transaction_id']:
        raise HTTPException(409, 'Номер Airpay не совпадает с уже подтверждённой оплатой.')
    amount_value(row.get('amount'), required=True)
    if not row.get('currency'):
        raise HTTPException(409, 'Не определена валюта покупки; требуется отдельная сверка.')
    if row['purchase_kind'] == 'voucher':
        if not code or len(code) > 4096 or any(ord(char) < 32 for char in code):
            raise HTTPException(422, 'Введите полученный у поставщика код ваучера без управляющих символов.')
        if code in evidence:
            raise HTTPException(422, 'Уберите код из основания решения: для него есть отдельное защищённое поле.')
        changes['pin_code'] = code
    elif code:
        raise HTTPException(422, 'Пополнение не должно содержать код ваучера.')
    return {**changes, 'state': 'paid', 'provider_transaction_id': provider_id,
            'provider_message': 'Владелец подтвердил успешную покупку по результатам внешней сверки.'}
