"""Покупка Airpay: сохранение запроса, повтор по тому же ID и отдельная выдача кода."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import HTTPException

from airpay_runtime.airpay_preparation import amount_value, normalize_check
from airpay_runtime.airpay_contract import transaction_outcome


FINAL_FAILURES = {4, 5, 7, 8, 130, 155, 202, 241, 242, 250, 300}
MAX_RECOVERY_ATTEMPTS = 5


def now_utc():
    # Одно представление времени используется для срока подготовки и пауз повторов.
    return datetime.now(timezone.utc)


def retry_at(attempt):
    # Серверная пауза переживает обновление страницы и перезапуск API.
    return now_utc() + timedelta(seconds=[5, 15, 30, 60, 300][min(max(attempt - 1, 0), 4)])


def wait_guard(row):
    # Не даём параллельным вкладкам или прямым API-запросам обходить паузу поставщика.
    if row.get('next_attempt_at') and row['next_attempt_at'] > now_utc():
        raise HTTPException(429, 'Следующий запрос Airpay пока недоступен. Повторите позже.')


def public_transaction(row, enabled):
    # Код исключается в самом контракте; раскрытие возможно только отдельным запросом с аудитом.
    return {key: row.get(key) for key in ('service_id', 'service_title', 'account', 'purchase_kind', 'state',
            'currency', 'provider_transaction_id', 'provider_message', 'created_at', 'updated_at',
            'expires_at', 'next_attempt_at', 'pay_attempts', 'voucher_attempts')} | {
        'agent_transaction_id': str(row['agent_transaction_id']),
        'batch': row['service_snapshot'].get('_airpay_batch'),
        'replaced': bool(row.get('replacement_key')), **transaction_outcome(row),
        'amount': str(row['amount']) if row.get('amount') is not None else '', 'payments_enabled': enabled,
    }


def quote_amount(result, raw, row):
    # fixedPrice — закупочная сумма из переписки; finalAmount относится к конвертации и не подставляется в pay.
    if result['scheme'] != 'simple':
        raise HTTPException(409, 'Для договоров и квитанций формат оплаты ещё не подтверждён Airpay')
    display_price = (raw.get('displays') or {}).get('fixedPrice')
    price = raw.get('fixedPrice', display_price)
    if price is not None:
        parsed = amount_value(price, required=True)
        if display_price is not None and amount_value(display_price, required=True) != parsed:
            raise HTTPException(502, 'Airpay вернул разные значения fixedPrice')
        return parsed
    if row['purchase_kind'] == 'voucher' or row['service_snapshot'].get('fixed_payment'):
        raise HTTPException(409, 'Airpay не вернул fixedPrice. Покупка фиксированной позиции недоступна.')
    return amount_value(result['fixed_amount'] or result['amount_to'], required=True)


class AirpayPurchase:
    def __init__(self, service, repository, preparation):
        # Транспорт и журнал подменяются в тестах; никаких фоновых покупок при создании объекта нет.
        self.service, self.repository, self.preparation = service, repository, preparation

    def check(self, owner, token):
        # Один check удерживает блокировку записи; после оплаты повторный check не меняет её состояние.
        draft = self.preparation.read_draft(owner, token)
        with self.repository.locked(owner, draft['request']['agentTransactionId']) as record:
            row = record.row
            if row['request_payload'] != draft['request']:
                raise HTTPException(409, 'Снимок подготовки не совпадает с журналом')
            if row['state'] in {'checked', 'check_failed', 'processing', 'paid', 'failed'}:
                return self.check_view(row)
            wait_guard(row)
            attempts = row['check_attempts'] + 1
            record.update(state='checking', check_attempts=attempts, next_attempt_at=retry_at(attempts))
            try:
                raw = self.service.check(row['request_payload'])
                result = normalize_check(raw, row['request_payload'], row['service_snapshot'])
            except HTTPException:
                record.update(state='check_pending', provider_message='Ответ check не получен; повторите проверку с прежним ID')
                raise
            amount, currency, reason = None, '', ''
            if result['success']:
                try:
                    amount = quote_amount(result, raw, row)
                    amount_from = row['request_payload'].get('amountFrom')
                    if amount_from is not None and Decimal(str(amount_from)) < Decimal(amount):
                        raise HTTPException(422, 'Принятая от клиента сумма меньше закупочной цены. Исправьте сумму и повторите подготовку.')
                    balance = self.service.get_balance()
                    currency = balance.get('currency', '')
                    if not balance.get('configured') or not currency:
                        raise HTTPException(409, 'Не удалось определить валюту агентского счёта')
                    if Decimal(str(balance['balance'])) < Decimal(amount):
                        raise HTTPException(409, 'Недостаточно средств на депозите поставщика')
                except HTTPException as exc:
                    reason = str(exc.detail)
            result.update(purchase_amount=amount or '', purchase_currency=currency, purchase_ready=result['success'] and not reason,
                          purchase_block_reason=reason, purchase_kind=row['purchase_kind'])
            record.update(check_response=raw, check_result=result, amount=amount, currency=currency,
                          state='checked' if result['success'] else 'check_pending' if result['retryable'] else 'check_failed',
                          provider_message=result['message'], next_attempt_at=retry_at(attempts) if result['retryable'] else None)
            return self.check_view(record.row)

    def check_view(self, row):
        # Восстанавливаем результат формы из БД, не создавая новый внешний запрос.
        return {**(row['check_result'] or {}), 'payments_enabled': self.service.payments_enabled,
                'transaction': public_transaction(row, self.service.payments_enabled)}

    def require_enabled(self):
        # Это дополнительная защита к запрету в транспорте, до любых изменений журнала.
        if not self.service.payments_enabled:
            raise HTTPException(403, 'Оплата и получение ваучеров Airpay отключены в этом окружении')

    def pay(self, owner, transaction_id, confirmed_amount, *, batch=False):
        # Первичная оплата принимает только ID и подтверждённую цену, остальные поля берутся из БД.
        self.require_enabled()
        with self.repository.locked(owner, transaction_id) as record:
            row = record.row
            if row['service_snapshot'].get('_airpay_batch') and not batch:
                raise HTTPException(409, 'Подтвердите покупку через итог всего количества')
            if row.get('replacement_key'):
                raise HTTPException(409, 'Позиция заменена новой подготовкой. Старую оплачивать нельзя.')
            if row['state'] in {'paid', 'failed'}:
                return public_transaction(row, True)
            if row['state'] == 'processing':
                raise HTTPException(409, 'Оплата уже отправлена. Уточните результат в истории.')
            if row['state'] != 'checked' or not (row['check_result'] or {}).get('purchase_ready'):
                raise HTTPException(409, 'Покупка недоступна без успешной подготовки')
            if row['expires_at'] <= now_utc():
                raise HTTPException(410, 'Цена устарела. Выполните новую подготовку.')
            amount = amount_value(confirmed_amount, required=True)
            if Decimal(amount) != Decimal(str(row['amount'])):
                raise HTTPException(409, 'Подтверждённая цена не совпадает с подготовкой')
            balance = self.service.get_balance()
            if balance.get('currency') != row['currency'] or not balance.get('configured') or Decimal(str(balance['balance'])) < Decimal(amount):
                raise HTTPException(409, 'Изменились баланс или валюта счёта. Обновите подготовку.')
            payload = {**row['request_payload'], 'amountTo': float(amount)}
            record.update(pay_request=payload, state='processing', provider_message='Оплата отправляется; ожидается результат')
            return self.send_pay(record)

    def reconcile(self, owner, transaction_id):
        # Документация требует повтор pay: строго тот же снимок, без новой операции и смены реквизитов.
        self.require_enabled()
        with self.repository.locked(owner, transaction_id) as record:
            if record.row['state'] != 'processing':
                return public_transaction(record.row, True)
            if record.row.get('requires_attention') or record.row['pay_attempts'] >= MAX_RECOVERY_ATTEMPTS:
                record.update(requires_attention=True, attention_reason='payment_unconfirmed')
                raise HTTPException(409, 'Нужна внешняя сверка с Airpay и ручное решение; повтор pay остановлен.')
            if not record.row['pay_request']:
                raise HTTPException(409, 'Нет сохранённого запроса оплаты; требуется ручная сверка')
            wait_guard(record.row)
            return self.send_pay(record)

    def send_pay(self, record):
        # После обрыва остаётся processing: неизвестный результат не превращается в разрешение купить снова.
        row = record.row
        attempts = row['pay_attempts'] + 1
        record.update(pay_attempts=attempts, next_attempt_at=retry_at(attempts))
        try:
            raw = self.service.pay(row['pay_request'])
            code = self.validate_response(raw, row['request_payload']['agentTransactionId'])
        except HTTPException:
            record.update(provider_message='Результат оплаты неизвестен. Уточните его в истории; новую покупку не создавайте.',
                          requires_attention=attempts >= MAX_RECOVERY_ATTEMPTS,
                          attention_reason='payment_unconfirmed' if attempts >= MAX_RECOVERY_ATTEMPTS else '')
            return public_transaction(record.row, True)
        # 202 после повтора, 215 и неизвестный код не доказывают отказ исходной операции.
        state = 'paid' if code == 0 else 'failed' if code in FINAL_FAILURES and not (code == 202 and attempts > 1) else 'processing'
        record.update(pay_response=raw, state=state, provider_transaction_id=str(raw.get('transactionId') or ''),
                      provider_message=str(raw.get('resultMessage') or '')[:2000],
                      next_attempt_at=retry_at(attempts) if state == 'processing' else None,
                      requires_attention=state == 'processing' and attempts >= MAX_RECOVERY_ATTEMPTS,
                      attention_reason='payment_unconfirmed' if state == 'processing' and attempts >= MAX_RECOVERY_ATTEMPTS else '')
        return public_transaction(record.row, True)

    def voucher(self, owner, transaction_id):
        # Выдача кода никогда не вызывает pay и не изменяет уже подтверждённую успешную оплату.
        self.require_enabled()
        with self.repository.locked(owner, transaction_id) as record:
            row = record.row
            if row['state'] != 'paid' or row['purchase_kind'] != 'voucher':
                raise HTTPException(409, 'Код доступен только после окончательно успешной оплаты ваучера')
            if row['pin_code']:
                return public_transaction(row, True)
            if row.get('requires_attention') or row['voucher_attempts'] >= MAX_RECOVERY_ATTEMPTS:
                record.update(requires_attention=True, attention_reason='voucher_missing')
                raise HTTPException(409, 'Код не получен. Нужна внешняя сверка и ручное сохранение результата.')
            wait_guard(row)
            attempts = row['voucher_attempts'] + 1
            record.update(voucher_attempts=attempts, next_attempt_at=retry_at(attempts))
            try:
                raw = self.service.get_voucher(row['pay_request'])
                code = self.validate_response(raw, row['request_payload']['agentTransactionId'])
                if code == 0 and str(raw.get('transactionId')) != row['provider_transaction_id']:
                    raise HTTPException(502, 'Код относится к другой операции поставщика')
                pin = (raw.get('displays') or {}).get('pinCode') if code == 0 and isinstance(raw.get('displays'), dict) else None
                if not isinstance(pin, str) or not pin.strip():
                    record.update(voucher_response=raw, provider_message='Оплачено. Код ещё не получен; повторите получение позже.')
                else:
                    record.update(voucher_response=raw, pin_code=pin.strip(), next_attempt_at=None, provider_message='Оплачено. Ваучер получен.')
            except HTTPException as exc:
                record.update(provider_message=f'Оплачено. Код не получен: {exc.detail}. Повторная оплата не нужна.'[:2000])
            if not record.row['pin_code'] and attempts >= MAX_RECOVERY_ATTEMPTS:
                record.update(requires_attention=True, attention_reason='voucher_missing')
            return public_transaction(record.row, True)

    def validate_response(self, raw, agent_id):
        # Успех принимается только для нашего ID с идентификатором платежа у поставщика.
        if not isinstance(raw, dict) or type(raw.get('result')) is not int:
            raise HTTPException(502, 'Некорректный ответ Airpay')
        if raw.get('agentTransactionId') is not None and str(raw['agentTransactionId']) != str(agent_id):
            raise HTTPException(502, 'Airpay вернул другую операцию')
        if raw['result'] == 0 and (str(raw.get('agentTransactionId')) != str(agent_id) or not raw.get('transactionId')):
            raise HTTPException(502, 'Airpay не подтвердил идентификаторы операции')
        return raw['result']
