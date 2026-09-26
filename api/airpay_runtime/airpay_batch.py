"""Последовательная покупка ваучеров: каждый ключ имеет собственный журнал и ID."""

from decimal import Decimal
import time
import uuid

import jwt
from fastapi import HTTPException

from airpay_runtime.airpay_service import available_airpay_funds
from airpay_runtime.airpay_preparation import DRAFT_AUDIENCE, amount_value
from airpay_runtime.airpay_purchase import now_utc, public_transaction


BATCH_META = '_airpay_batch'


def preparation_keys(key, quantity):
    # Детерминированные ключи восстанавливают те же операции после оборванной подготовки.
    return [str(key)] + [str(uuid.uuid5(uuid.UUID(str(key)), f'airpay-voucher-{index}')) for index in range(1, quantity)]


def batch_metadata(row):
    # Метаданные принадлежат приложению и не передаются в запросы поставщика.
    return row['service_snapshot'].get(BATCH_META) or {}


def batch_view(rows, enabled):
    # Итог строится по сохранённым операциям, а не по счётчику в памяти браузера.
    states = [row['state'] for row in rows]
    paid = sum(state == 'paid' for state in states)
    state = ('processing' if 'processing' in states else 'failed' if 'failed' in states else
             'paid' if paid == len(rows) else 'partial' if paid else
             'checked' if all(state == 'checked' for state in states) else 'prepared')
    total = sum((Decimal(str(row['amount'])) for row in rows if row.get('amount') is not None), Decimal(0))
    return {'batch_id': str(rows[0]['agent_transaction_id']), 'quantity': len(rows), 'state': state,
            'replaced': any(row.get('replacement_key') for row in rows),
            'remaining_amount': format(sum((Decimal(str(row['amount'] or 0)) for row in rows if row['state'] != 'paid'), Decimal(0)), '.2f'),
            'paid_quantity': paid, 'received_quantity': sum(bool(row['pin_code']) for row in rows),
            'total_amount': format(total, '.2f') if all(row.get('amount') is not None for row in rows) else '',
            'currency': rows[0]['currency'], 'payments_enabled': enabled,
            'items': [public_transaction(row, enabled) for row in rows]}


class AirpayBatch:
    def __init__(self, service, repository, preparation, purchase):
        # Используем существующий журнал и защиту одиночной покупки для каждого ключа.
        self.service, self.repository, self.preparation, self.purchase = service, repository, preparation, purchase

    def read(self, owner, transaction_id):
        # Чтение пачки не вызывает внешних методов и доступно при локальном запрете оплаты.
        row = self.repository.batch_root(owner, transaction_id)
        return self.view(owner, self.repository.batch_rows(owner, row))

    def view(self, owner, rows):
        # Ссылка на переоценённый остаток нужна после закрытия формы или перезапуска.
        return {**batch_view(rows, self.service.payments_enabled), 'replacement_id': self.repository.replacement(owner, rows[0]['agent_transaction_id'])}

    def saved_token(self, owner, transaction_id):
        # История восстанавливает ту же проверку после закрытия вкладки, без нового ID и новой даты.
        row = self.repository.batch_root(owner, transaction_id)
        if row['expires_at'] <= now_utc():
            raise HTTPException(410, 'Подготовка истекла. Проверьте неоплаченный остаток заново.')
        return jwt.encode({'aud': DRAFT_AUDIENCE, 'sub': owner, 'iat': int(row['expires_at'].timestamp()) - 900,
            'exp': int(row['expires_at'].timestamp()), 'request': row['request_payload'],
            'service': row['service_snapshot']}, self.preparation.get_secret(), algorithm='HS256')

    def check(self, owner, token, *, progress=lambda value: None):
        # Повтор проверяет лишь незавершённые позиции; успешные check читаются из журнала.
        draft = self.preparation.read_draft(owner, token)
        transaction_id = draft['request']['agentTransactionId']
        with self.repository.batch_locked(owner, transaction_id) as rows:
            for index, row in enumerate(rows):
                if row.get('replacement_key'):
                    raise HTTPException(409, 'Проверка заменена новой подготовкой остатка')
                if row['state'] in {'processing', 'paid', 'failed'}:
                    raise HTTPException(409, 'Покупка уже начата. Откройте сохранённые результаты.')
                if row['expires_at'] <= now_utc():
                    raise HTTPException(410, 'Срок проверки истёк. Подготовьте новую проверку.')
                signed = jwt.encode({'aud': DRAFT_AUDIENCE, 'sub': owner, 'iat': int(time.time()),
                    'exp': int(row['expires_at'].timestamp()), 'request': row['request_payload'],
                    'service': row['service_snapshot']}, self.preparation.get_secret(), algorithm='HS256')
                result = self.purchase.check(owner, signed)
                progress(index + 1)
                if not result.get('success'):
                    return {**result, 'quantity': len(rows)}
            rows = self.repository.batch_rows(owner, rows[0])
            first = rows[0]['check_result']
            total = sum((Decimal(str(row['amount'] or 0)) for row in rows), Decimal(0))
            currencies = {row['currency'] for row in rows}
            reason = next((row['check_result'].get('purchase_block_reason') for row in rows
                           if not row['check_result'].get('purchase_ready')), '')
            if not reason and (len(currencies) != 1 or not all(row.get('amount') for row in rows)):
                reason = 'Не удалось определить общую цену и валюту покупки'
            if not reason:
                balance = self.service.get_balance()
                # Всю пачку сравниваем с единым доступным остатком с учётом кредита.
                if not balance.get('configured') or balance.get('currency') not in currencies or available_airpay_funds(balance) < total:
                    reason = 'Недостаточно средств на депозите поставщика для всего количества'
            prices = {str(row['amount']) for row in rows}
            return {**first, 'quantity': len(rows), 'purchase_amount': format(total, '.2f'),
                    'unit_amount': str(rows[0]['amount']) if len(prices) == 1 and rows[0]['amount'] is not None else '',
                    'purchase_ready': not bool(reason), 'purchase_block_reason': reason,
                    'payments_enabled': self.service.payments_enabled,
                    'transaction': public_transaction(rows[0], self.service.payments_enabled),
                    'batch': batch_view(rows, self.service.payments_enabled)}

    def pay(self, owner, transaction_id, confirmed_amount, *, progress=lambda value: None):
        # Блокировка пачки не даёт двум вкладкам покупать следующие позиции одновременно.
        self.purchase.require_enabled()
        with self.repository.batch_locked(owner, transaction_id) as rows:
            if any(row.get('replacement_key') for row in rows):
                raise HTTPException(409, 'Неоплаченные позиции заменены новой подготовкой')
            if any(row['state'] in {'processing', 'failed'} for row in rows) or all(row['state'] == 'paid' for row in rows):
                return batch_view(rows, True)
            if any(row['state'] not in {'checked', 'paid'} or not (row['check_result'] or {}).get('purchase_ready') for row in rows):
                raise HTTPException(409, 'Не все ключи прошли подготовку')
            total = sum((Decimal(str(row['amount'])) for row in rows), Decimal(0))
            if Decimal(amount_value(confirmed_amount, required=True, maximum='19999999999.80')) != total:
                raise HTTPException(409, 'Подтверждённая общая цена не совпадает с подготовкой')
            pending = [row for row in rows if row['state'] != 'paid']
            if any(row['expires_at'] <= now_utc() for row in pending):
                raise HTTPException(410, 'Цена устарела. Неоплаченные позиции требуют новой подготовки.')
            balance = self.service.get_balance()
            # Уже оплаченные позиции повторно не расходуют доступный лимит при продолжении.
            remaining = sum((Decimal(str(row['amount'])) for row in pending), Decimal(0))
            if not balance.get('configured') or any(row['currency'] != balance.get('currency') for row in rows) or available_airpay_funds(balance) < remaining:
                raise HTTPException(409, 'Недостаточно средств для оставшегося количества или изменилась валюта')
            progress(len(rows) - len(pending))
            for index, row in enumerate(pending):
                result = self.purchase.pay(owner, row['agent_transaction_id'], str(row['amount']), batch=True)
                progress(len(rows) - len(pending) + index + 1)
                if result['state'] != 'paid':
                    break
            return batch_view(self.repository.batch_rows(owner, rows[0]), True)

    def vouchers(self, owner, transaction_id, *, progress=lambda value: None):
        # Получаем только коды оплаченных позиций; пропущенные и неизвестные оплаты не повторяем.
        self.purchase.require_enabled()
        with self.repository.batch_locked(owner, transaction_id) as rows:
            for index, row in enumerate(rows):
                if row['state'] == 'paid' and not row['pin_code']:
                    result = self.purchase.voucher(owner, row['agent_transaction_id'])
                    if not result['result_available']:
                        break
                progress(index + 1)
            return batch_view(self.repository.batch_rows(owner, rows[0]), True)

    def renew(self, owner, transaction_id):
        # Новые ID создаются лишь для доказанно неоплаченного остатка; processing не заменяется.
        with self.repository.batch_locked(owner, transaction_id) as rows:
            renewal = self.repository.renew(owner, rows)
            row = self.repository.read(owner, renewal['source_transaction_id'])
            fields = {}
            request = row['request_payload']
            for field in row['service_snapshot']['inputs']:
                name = field['name']
                fields[name] = str(request.get(name, '') if name in {'account', 'identityCard', 'fullName'} else
                                   request.get('extras', {}).get(name[3:] if name.startswith('ev_') else name, ''))
            fields['account'] = request['account']
            draft = self.preparation.prepare(owner, row['service_id'], fields, purchase_kind='voucher',
                preparation_key=str(renewal['preparation_key']), quantity=renewal['quantity'], force_batch=True)
            self.repository.finish_renewal(owner, rows[0]['agent_transaction_id'], int(draft['agent_transaction_id']))
            return {'draft': draft, 'batch': self.read(owner, int(draft['agent_transaction_id']))}
