"""Подготовка и проверка Airpay без возможности отправить pay."""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import secrets
import subprocess
import sys
import time
import uuid

import jwt
from fastapi import HTTPException

TEMPORARY_RESULTS = {1, 153, 220, 255}
DRAFT_AUDIENCE = 'airpay-preparation'


def amount_value(value, *, required=False, maximum='999999999.99'):
    # Сохраняем копейки точно и не принимаем нулевую, отрицательную или дробную сверх сотых сумму.
    if value in (None, '') and not required:
        return None
    try:
        if isinstance(value, bool):
            raise ValueError
        amount = Decimal(str(value))
        if not amount.is_finite() or amount <= 0 or amount > Decimal(maximum) or amount != amount.quantize(Decimal('.01')):
            raise ValueError
    except (ValueError, InvalidOperation, TypeError):
        raise HTTPException(422, 'Сумма должна быть положительным числом с точностью до копеек') from None
    return format(amount, '.2f')


def validate_patterns(checks):
    # Проверяем выражения поставщика отдельно от API: сложный regexp не может надолго занять его поток.
    if not checks:
        return
    script = '''import json, re, sys
checks = json.load(sys.stdin)
failed = [name for name, pattern, value in checks if re.fullmatch(pattern, value) is None]
print(json.dumps(failed))
'''
    try:
        result = subprocess.run([sys.executable, '-I', '-c', script], input=json.dumps(checks),
                                capture_output=True, text=True, timeout=1, check=True)
        failed = json.loads(result.stdout)
    except (subprocess.SubprocessError, ValueError):
        raise HTTPException(422, 'Не удалось проверить формат полей услуги; уточните правила у Airpay') from None
    if failed:
        raise HTTPException(422, 'Проверьте формат: ' + ', '.join(failed))


def build_check_payload(service, fields, amount_to=None, amount_from=None):
    # Маршрутизируем поля согласно check: основной account и идентификация наверху, остальные в extras.
    definitions = {field['name']: field for field in service['inputs']}
    if len(definitions) != len(service['inputs']):
        raise HTTPException(502, 'Airpay вернул повторяющиеся поля услуги')
    definitions.setdefault('account', {'name': 'account', 'title': 'Идентификатор', 'required': True, 'regexp': ''})
    if set(fields) - set(definitions):
        raise HTTPException(422, 'В форме есть неизвестные поля услуги')
    payload = {'serviceId': service['service_id']}
    extras, checks = {}, []
    for name, field in definitions.items():
        value = fields.get(name, '').strip()
        title = field['title'] or name
        if not value:
            if field['required'] or name == 'account':
                raise HTTPException(422, f'Заполните поле «{title}»')
            continue
        if field['regexp']:
            checks.append((title, field['regexp'], value))
        if name in {'account', 'identityCard', 'fullName'}:
            payload[name] = value
        else:
            # В примере services ev_account1 соответствует account1 в контейнере extras метода check.
            extra_name = name[3:] if name.startswith('ev_') else name
            if not extra_name or extra_name in extras:
                raise HTTPException(502, 'Airpay вернул неоднозначные дополнительные поля')
            extras[extra_name] = value
    validate_patterns(checks)
    if extras:
        payload['extras'] = extras
    for key, value in (('amountTo', amount_to), ('amountFrom', amount_from)):
        parsed = amount_value(value)
        if parsed is not None:
            payload[key] = float(parsed)
    if 'amountTo' in payload and 'amountFrom' in payload and payload['amountFrom'] < payload['amountTo']:
        raise HTTPException(422, 'Принятая от клиента сумма не может быть меньше суммы к зачислению')
    return payload


class AirpayPreparation:
    def __init__(self, service, get_secret, repository=None):
        # Подпись связывает форму с сохранённой операцией; журнал нужен для восстановления покупки.
        self.service = service
        self.get_secret = get_secret
        self.repository = repository

    def prepare(self, owner, service_id, fields, amount_to=None, amount_from=None, *, purchase_kind=None, preparation_key=None, quantity=1, force_batch=False):
        # Новая проверка получает серверные ID и дату; браузер не может подменить их при повторе.
        secret = self.get_secret()
        if not secret:
            raise HTTPException(503, 'Не настроена подпись подготовки Airpay')
        if type(quantity) is not int or not 1 <= quantity <= 20:
            raise HTTPException(422, 'Количество ключей должно быть целым числом от 1 до 20')
        service = self.service.get_service(service_id)
        # Тип и сумма определяются свежим описанием услуги, а не выбором браузера.
        fixed_payment = service.get('fixed_payment')
        if type(fixed_payment) is not bool:
            raise HTTPException(422, 'Airpay не указал вид суммы платежа для этой услуги')
        inferred_kind = 'voucher' if fixed_payment else 'topup'
        if purchase_kind is not None and purchase_kind != inferred_kind:
            raise HTTPException(422, 'Тип покупки изменился. Обновите параметры услуги')
        purchase_kind = inferred_kind
        if quantity > 1 and purchase_kind != 'voucher':
            raise HTTPException(422, 'Количество доступно только для ваучеров')
        if purchase_kind == 'voucher' and amount_to not in (None, ''):
            raise HTTPException(422, 'Цену ваучера определяет Airpay при проверке')
        if purchase_kind == 'topup':
            if amount_to in (None, ''):
                raise HTTPException(422, 'Укажите сумму к зачислению')
            amount_to = amount_value(amount_to, required=True)
        payload = build_check_payload(service, fields, amount_to, amount_from)
        now = int(time.time())
        expires = now + 900
        key = preparation_key or str(uuid.uuid4())
        if quantity > 1 and self.repository is None:
            raise HTTPException(503, 'Журнал операций Airpay не подключён')
        # Связь пачки хранится в существующем JSON-снимке, каждый ключ — отдельная строка журнала.
        from airpay_runtime.airpay_batch import BATCH_META, preparation_keys
        rows = []
        for index, item_key in enumerate(preparation_keys(key, quantity)):
            request = {**payload, 'agentTransactionId': secrets.randbelow(2 ** 63 - 1) + 1,
                       'agentTransactionDate': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')}
            snapshot = {**service}
            if quantity > 1 or force_batch:
                snapshot[BATCH_META] = {'key': key, 'quantity': quantity, 'index': index}
            if self.repository is not None:
                rows.append(self.repository.create(owner, item_key, request, snapshot, purchase_kind, expires))
            else:
                payload = request
        if rows:
            payload, service = rows[0]['request_payload'], rows[0]['service_snapshot']
            expires = min(int(row['expires_at'].timestamp()) for row in rows)
        token = jwt.encode({'aud': DRAFT_AUDIENCE, 'sub': owner, 'iat': now, 'exp': expires,
                            'request': payload, 'service': service}, secret, algorithm='HS256')
        if len(token) > 100000:
            raise HTTPException(502, 'Описание услуги Airpay слишком большое для подготовки')
        return {'preparation_token': token, 'agent_transaction_id': str(payload['agentTransactionId']),
                'agent_transaction_date': payload['agentTransactionDate'], 'expires_at': expires, 'quantity': quantity}

    def check(self, owner, token):
        # Повтор всегда отправляет неизменный запрос прежнего check, даже после перезапуска API.
        draft = self.read_draft(owner, token)
        response = self.service.check(draft['request'])
        return normalize_check(response, draft['request'], draft['service'])

    def read_draft(self, owner, token):
        # Проверяем сеанс и срок до чтения журнала или передачи реквизитов поставщику.
        try:
            draft = jwt.decode(token, self.get_secret(), algorithms=['HS256'], audience=DRAFT_AUDIENCE,
                               options={'require': ['aud', 'sub', 'iat', 'exp', 'request', 'service']})
        except jwt.ExpiredSignatureError:
            raise HTTPException(410, 'Срок проверки истёк. Подготовьте новую проверку.') from None
        except jwt.PyJWTError:
            raise HTTPException(422, 'Некорректная подготовка Airpay') from None
        if draft['sub'] != owner:
            raise HTTPException(403, 'Эта проверка принадлежит другому пользователю')
        return draft


def normalize_check(response, request, service):
    # Сохраняем документированные варианты ответа, а неизвестный код не считаем успехом.
    if not isinstance(response, dict) or type(response.get('result')) is not int:
        raise HTTPException(502, 'Airpay вернул некорректный результат проверки')
    code = response['result']
    if code == 0 and (str(response.get('agentTransactionId')) != str(request['agentTransactionId'])
                      or not response.get('transactionId')):
        raise HTTPException(502, 'Airpay вернул подтверждение другой или неизвестной операции')
    displays = response.get('displays') or {}
    contracts = response.get('contracts')
    invoice = response.get('invoice')
    if not isinstance(displays, dict) or (contracts is not None and not isinstance(contracts, list)) or (invoice is not None and not isinstance(invoice, dict)):
        raise HTTPException(502, 'Airpay вернул некорректные данные проверки')
    if contracts is not None and any(not isinstance(item, dict) for item in contracts):
        raise HTTPException(502, 'Airpay вернул некорректный список договоров')
    if invoice is not None:
        invoices = invoice.get('invoices')
        if not isinstance(invoices, list) or any(not isinstance(item, dict) or not isinstance(item.get('services'), list)
            or any(not isinstance(row, dict) or (row.get('data') is not None and not isinstance(row['data'], dict))
                   for row in item['services']) for item in invoices):
            raise HTTPException(502, 'Airpay вернул некорректные квитанции')
    titles = {field['name']: field['title'] or field['name'] for field in service['displays']}
    return {'success': code == 0, 'result': code, 'message': str(response.get('resultMessage') or '')[:2000],
            'retryable': code in TEMPORARY_RESULTS, 'agent_transaction_id': str(request['agentTransactionId']),
            'transaction_id': str(response.get('transactionId') or ''),
            'amount_to': str(request.get('amountTo', '')), 'amount_from': str(request.get('amountFrom', '')),
            'displays': [{'name': str(name), 'title': titles.get(name, str(name)), 'value': str(value)}
                         for name, value in displays.items() if value is not None and isinstance(value, (str, int, float))],
            'fixed_amount': str(displays.get('fixedAmount') or ''),
            'fixed_price': str(response.get('fixedPrice', displays.get('fixedPrice', '')) or ''),
            'currency_rate': str(response.get('currencyRate') or ''), 'rate': str(response.get('rate') or ''),
            'final_amount': str(response.get('finalAmount') or ''), 'currency': str(response.get('currency') or ''),
            'contracts': contracts, 'invoice': invoice,
            'scheme': 'invoice' if invoice is not None else 'contracts' if contracts is not None else 'simple'}
