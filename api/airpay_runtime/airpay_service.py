"""Транспорт Airpay с серверной Basic-авторизацией и запретом локальной оплаты."""

import base64
import json
import math
from decimal import Decimal, InvalidOperation
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlsplit

from fastapi import HTTPException
from airpay_runtime.transport import NoTunnelRedirects, TunnelHTTPSHandler


AIRPAY_API_URL = 'https://api.airpay.kz/AirPayService/api/v20'


@dataclass
class AirpayService:
    get_balance: Callable[[], dict]
    get_services: Callable[[], dict]
    get_service: Callable[[str], dict]
    check: Callable[[dict], dict]
    pay: Callable[[dict], dict]
    get_voucher: Callable[[dict], dict]
    payments_enabled: bool


def build_airpay_service(environ, *, local_ui=False):
    # Читаем отдельные серверные настройки, не смешивая реквизиты двух поставщиков.
    api_url = environ.get('AIRPAY_API_URL', AIRPAY_API_URL).rstrip('/')
    username = environ.get('AIRPAY_USERNAME', '')
    password = environ.get('AIRPAY_PASSWORD', '')
    proxy_url = environ.get('AIRPAY_PROXY_URL', '').strip()
    tunnel_port = environ.get('AIRPAY_TUNNEL_LOCAL_PORT', '3129')
    ca_cert_path = environ.get('AIRPAY_CA_CERT_PATH', '')
    ssl_verify_setting = str(environ.get('AIRPAY_SSL_VERIFY', 'true') or 'true').strip().lower()
    timeout = environ.get('AIRPAY_TIMEOUT_SEC', '20')
    voucher_url = environ.get('AIRPAY_VOUCHER_URL', 'https://partner.airpay.kz:9969/api/Payment/getVoucherCodeByPaymentId').strip()
    payments_enabled = (not local_ui and str(environ.get('GAMESALES_SUPPLIER_OFFLINE', '')).lower() not in {'1', 'true', 'yes', 'on'}
                        and str(environ.get('AIRPAY_PAYMENTS_ENABLED', '')).lower() in {'1', 'true', 'yes', 'on'})

    def send_request(path, body=None):
        # Локальный режим запрещает оплату и выдачу кода даже при прямом вызове транспорта.
        if path not in {'balance', 'services', 'service', 'check', 'pay', 'voucher'}:
            raise HTTPException(403, 'Метод Airpay не разрешён')
        if path in {'pay', 'voucher'} and not payments_enabled:
            raise HTTPException(403, 'Оплата и получение ваучеров Airpay отключены в этом окружении')
        if not username or not password:
            raise HTTPException(503, 'Airpay ещё не подключён')
        try:
            url = voucher_url if path == 'voucher' else f'{api_url}/{path}'
            target = urlsplit(url)
            if target.scheme != 'https' or not target.hostname or target.username or target.password or target.query or target.fragment or ':' in username:
                raise ValueError
            request_timeout = int(timeout)
            if not 1 <= request_timeout <= 120:
                raise ValueError
            port = int(tunnel_port) if local_ui else None
            if port is not None and not 1 <= port <= 65535:
                raise ValueError
            # По умолчанию проверяем сертификат; отключение действует только по явному значению настройки.
            if ssl_verify_setting not in {'1', 'true', 'yes', 'on', '0', 'false', 'no', 'off'}:
                raise ValueError
            ssl_verify = ssl_verify_setting in {'1', 'true', 'yes', 'on'}
            context = ssl.create_default_context(cafile=ca_cert_path or None) if ssl_verify else ssl._create_unverified_context()
        except (ValueError, TypeError, OSError):
            raise HTTPException(503, 'Проверьте серверные настройки подключения Airpay') from None

        # Сохраняем исходные Host/SNI и выбранную настройку TLS при подключении через SSH-forward.
        proxy = {} if local_ui or not proxy_url else {'https': proxy_url}
        https_handler = (TunnelHTTPSHandler(context=context, tunnel_port=port) if local_ui
                         else urllib.request.HTTPSHandler(context=context))
        opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxy), https_handler, NoTunnelRedirects())
        credentials = base64.b64encode(f'{username}:{password}'.encode('utf-8')).decode('ascii')
        # Суммы передаём числовыми JSON-значениями с двумя знаками, как требует контракт Airpay.
        data = b'' if body is None else ('{' + ','.join(
            json.dumps(key) + ':' + (format(Decimal(str(value)), '.2f') if key in {'amountTo', 'amountFrom'}
                                    else json.dumps(value, ensure_ascii=False, allow_nan=False))
            for key, value in body.items()) + '}').encode('utf-8')
        request = urllib.request.Request(url, data=data, method='POST', headers={
            'Accept': 'application/json', 'Content-Type': 'application/json', 'Authorization': f'Basic {credentials}',
        })
        try:
            with opener.open(request, timeout=request_timeout) as response:
                payload = json.loads(response.read().decode('utf-8'))
        except urllib.error.HTTPError as exc:
            # Текст внешнего ответа и адрес proxy не отдаём клиенту: они могут содержать реквизиты.
            message = ('Airpay отклонил авторизацию. Проверьте логин, пароль и доступ по IP.'
                       if exc.code in (401, 403) else 'Airpay не удалось вернуть данные. Повторите запрос позже.')
            if path == 'voucher' and exc.code in (400, 404):
                message = 'Сервис ваучеров Airpay не нашёл платёж или отклонил его параметры. Проверьте операцию у поставщика.'
            raise HTTPException(502, message) from None
        except TimeoutError:
            raise HTTPException(504, 'Airpay не ответил вовремя. Повторите запрос.') from None
        except (urllib.error.URLError, OSError):
            raise HTTPException(502, 'Нет соединения с Airpay. Проверьте туннель или прокси.') from None
        except (ValueError, UnicodeError):
            raise HTTPException(502, 'Airpay вернул некорректный ответ') from None
        return payload

    def get_balance():
        # Отсутствие реквизитов отличается от реального нулевого остатка на счёте.
        if not username or not password:
            return {'configured': False, 'balance': None, 'overdraft': None, 'currency': ''}
        return normalize_airpay_balance(send_request('balance'))

    def get_services():
        # Каталог имеет собственное состояние настройки и не подменяется услугами Interhub.
        if not username or not password:
            return {'configured': False, 'items': [], 'total': 0}
        items = normalize_airpay_services(send_request('services'))
        return {'configured': True, 'items': items, 'total': len(items)}

    def get_service(service_id):
        # Перед формой читаем актуальные поля именно выбранной услуги, не доверяя снимку браузера.
        items = normalize_airpay_services(send_request('service', {'serviceId': service_id}))
        matched = [item for item in items if item['service_id'] == service_id]
        if len(matched) != 1:
            raise HTTPException(502, 'Airpay не вернул параметры выбранной услуги')
        return matched[0]

    def check(payload):
        # Проверка не списывает средства; повтор использует неизменный снимок запроса.
        return send_request('check', payload)

    def pay(payload):
        # Повтор оплаты получает только сохранённое тело; новый ID здесь не создаётся.
        return send_request('pay', payload)

    def get_voucher(payload):
        # Отдельный сервис ждёт то же тело pay, но ID агента обязательно строкой.
        return send_request('voucher', {**payload, 'agentTransactionId': str(payload['agentTransactionId'])})

    return AirpayService(get_balance=get_balance, get_services=get_services, get_service=get_service, check=check,
                         pay=pay, get_voucher=get_voucher, payments_enabled=payments_enabled)


def available_airpay_funds(balance):
    # Разрешённый овердрафт увеличивает доступную сумму; отрицательный баланс уже отражает использованный кредит.
    try:
        raw_balance, raw_overdraft = balance['balance'], balance.get('overdraft', 0)
        if not balance.get('configured') or isinstance(raw_balance, bool) or isinstance(raw_overdraft, bool):
            raise ValueError
        current, credit = Decimal(str(raw_balance)), Decimal(str(raw_overdraft))
        if not current.is_finite() or not credit.is_finite() or credit < 0:
            raise ValueError
    except (KeyError, TypeError, ValueError, InvalidOperation):
        raise HTTPException(502, 'Airpay вернул некорректный баланс или овердрафт') from None
    return current + credit


def normalize_airpay_balance(payload):
    # Принимаем только успешный ответ agent, не превращая ошибку или пропуск суммы в ноль.
    if not isinstance(payload, dict) or type(payload.get('result')) is not int or payload['result'] != 0:
        raise HTTPException(502, 'Airpay не подтвердил получение баланса')
    agent = payload.get('agent')
    try:
        if not isinstance(agent, dict):
            raise ValueError
        amounts = []
        for field in ('balance', 'overdraft'):
            if isinstance(agent.get(field), bool):
                raise ValueError
            amount = float(agent[field])
            if not math.isfinite(amount):
                raise ValueError
            amounts.append(amount)
        currency = str(agent.get('currency') or '').strip().upper()
        if currency and (len(currency) != 3 or not currency.isascii() or not currency.isalpha()):
            raise ValueError
    except (KeyError, ValueError, TypeError, OverflowError):
        raise HTTPException(502, 'Airpay вернул некорректные сумму или валюту баланса') from None
    return {'configured': True, 'balance': amounts[0], 'overdraft': amounts[1], 'currency': currency}


def normalize_airpay_fields(fields):
    # Сохраняем описание параметров; регулярные выражения поставщика пока не выполняем.
    if fields is None:
        return []
    if not isinstance(fields, list):
        raise ValueError
    result = []
    for field in fields:
        if not isinstance(field, dict) or not isinstance(field.get('name'), str) or not field['name'].strip():
            raise ValueError
        required = field.get('required', False)
        if not isinstance(required, bool):
            raise ValueError
        result.append({'name': field['name'].strip(), 'title': str(field.get('title') or '').strip(),
                       'required': required, 'regexp': str(field.get('regexp') or '')})
    return result


def normalize_airpay_services(payload):
    # Проверяем весь ответ, чтобы повреждённый каталог не выглядел успешным пустым списком.
    if not isinstance(payload, dict) or type(payload.get('result')) is not int or payload['result'] != 0:
        raise HTTPException(502, 'Airpay не подтвердил получение каталога')
    try:
        if not isinstance(payload.get('services'), list):
            raise ValueError
        items = []
        ids = set()
        for service in payload['services']:
            if not isinstance(service, dict):
                raise ValueError
            service_id = service.get('serviceId')
            if type(service_id) not in (str, int) or not str(service_id).strip():
                raise ValueError
            service_id = str(service_id).strip()
            if service_id in ids or not isinstance(service.get('name'), str) or not service['name'].strip():
                raise ValueError
            if type(service.get('type')) is not int:
                raise ValueError
            fixed = service.get('fixedPayment')
            if fixed is not None and not isinstance(fixed, bool):
                raise ValueError
            ids.add(service_id)
            items.append({'service_id': service_id, 'title': service['name'].strip(), 'type': service['type'],
                          'group': str(service.get('group') or ''), 'country': str(service.get('country') or ''),
                          'fixed_payment': fixed, 'inputs': normalize_airpay_fields(service.get('inputs')),
                          'displays': normalize_airpay_fields(service.get('displays'))})
    except (TypeError, ValueError):
        raise HTTPException(502, 'Airpay вернул некорректный каталог услуг') from None
    return items
