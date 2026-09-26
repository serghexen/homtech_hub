"""Последовательный диагностический check: журнал отделён от подготовки покупки."""
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import re
import secrets

from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from airpay_runtime.airpay_preparation import build_check_payload

DIAGNOSTIC_LOCK = 734208691263501
DEFAULT_EMAIL = 'seller@homtech.ru'


def diagnostic_payload(service):
    # Не угадываем аккаунт или сумму: автоматически проверяем только явное email-поле фиксированной услуги.
    if service.get('fixed_payment') is not True:
        return None, 'Нужна сумма и реквизиты для ручной проверки'
    account = next((field for field in service['inputs'] if field['name'] == 'account'), None)
    if not account or not re.search(r'e[\s-]?mail|почт', account.get('title', ''), re.I):
        return None, 'Нужен аккаунт получателя; служебная почта не подставляется'
    missing = [field['title'] or field['name'] for field in service['inputs'] if field['required'] and field['name'] != 'account']
    if missing:
        return None, 'Нужны реквизиты: ' + ', '.join(missing)
    try:
        return build_check_payload(service, {'account': DEFAULT_EMAIL}), ''
    except HTTPException as exc:
        return None, str(exc.detail)


def diagnostic_result(raw):
    # Храним только диагностические поля; ответ check не превращаем в разрешение оплаты или наличие товара.
    if not isinstance(raw, dict) or type(raw.get('result')) is not int:
        return {'state': 'invalid_response', 'message': 'Airpay вернул ответ без целого result'}
    code = raw['result']
    report = {'state': 'ok' if code == 0 else 'pending_response' if code in {1, 153, 220, 255} else 'rejected',
              'result': code, 'message': str(raw.get('resultMessage') or '')[:1000],
              'provider_transaction_id': str(raw.get('transactionId') or '')[:128]}
    displays = raw.get('displays') if isinstance(raw.get('displays'), dict) else {}
    price = raw.get('fixedPrice', displays.get('fixedPrice'))
    if price is not None:
        try:
            amount = Decimal(str(price))
            if isinstance(price, bool) or not amount.is_finite() or amount <= 0:
                raise ValueError
            if raw.get('fixedPrice') is not None and displays.get('fixedPrice') is not None and Decimal(str(displays['fixedPrice'])) != amount:
                raise ValueError
            report['fixed_price'] = str(amount)
        except (InvalidOperation, ValueError):
            report['price_warning'] = 'Некорректный или противоречивый fixedPrice'
    elif code == 0:
        report['price_warning'] = 'Успешный check без fixedPrice: закупочная цена не подтверждена'
    for source, target in [('currencyRate', 'conversion'), ('currency', 'provider_currency'), ('finalAmount', 'final_amount')]:
        if isinstance(raw.get(source), (str, int, float)):
            report[target] = str(raw[source])[:300]
    return report


class AirpayDiagnosticStore:
    def __init__(self, connect):
        # Используем отдельные таблицы с тем же подключением, без изменений платёжного журнала.
        self.connect = connect

    @contextmanager
    def locked(self):
        # Один опрос и один сетевой шаг на весь Airpay; блокировка снимается даже при падении процесса.
        with self.connect(autocommit=True, row_factory=dict_row) as conn:
            if not conn.execute('SELECT pg_try_advisory_lock(%s) AS acquired', (DIAGNOSTIC_LOCK,)).fetchone()['acquired']:
                raise HTTPException(409, 'Диагностический запрос Airpay уже выполняется')
            try:
                yield conn
            finally:
                conn.execute('SELECT pg_advisory_unlock(%s)', (DIAGNOSTIC_LOCK,))

    def read(self, owner, run_id=None, conn=None):
        # В отчёт попадают только операции текущего владельца; чтение никогда не обращается к Airpay.
        if conn is None:
            with self.connect(autocommit=True, row_factory=dict_row) as connection:
                return self.read(owner, run_id, connection)
        run = conn.execute('SELECT * FROM supplier_hub_airpay.airpay_diagnostic_runs WHERE created_by=%s ' +
                           ('AND id=%s' if run_id else 'ORDER BY created_at DESC LIMIT 1'),
                           (owner, run_id) if run_id else (owner,)).fetchone()
        if not run:
            if run_id:
                raise HTTPException(404, 'Отчёт диагностики не найден')
            return {'run': None}
        rows = conn.execute('SELECT * FROM supplier_hub_airpay.airpay_diagnostic_items WHERE run_id=%s ORDER BY position', (run['id'],)).fetchall()
        items = [{**row['report'], 'service_id': row['service_id'], 'title': row['title'], 'state': row['state'],
                  'agent_transaction_id': str(row['agent_transaction_id']) if row['agent_transaction_id'] else '',
                  'started_at': row['started_at'], 'finished_at': row['finished_at']} for row in rows]
        return {'run': {**run, 'id': str(run['id']), 'items': items,
                        'total': len(items), 'processed': sum(row['state'] not in {'pending', 'running'} for row in rows)}}


class AirpayDiagnostics:
    def __init__(self, store, *, get_services, get_service, check):
        # Платёжный транспорт намеренно не передаётся: доступны только справочники и check.
        self.store, self.get_services, self.get_service, self.check = store, get_services, get_service, check

    def start(self, owner, run_id):
        # UUID запуска делает повтор после потери ответа безопасным и не создаёт второй опрос.
        with self.store.locked() as conn:
            exists = conn.execute('SELECT id FROM supplier_hub_airpay.airpay_diagnostic_runs WHERE id=%s', (run_id,)).fetchone()
            if exists:
                return self.store.read(owner, run_id, conn)
            if conn.execute("SELECT id FROM supplier_hub_airpay.airpay_diagnostic_runs WHERE state='active'").fetchone():
                raise HTTPException(409, 'Есть незавершённый опрос Airpay. Продолжите или завершите его.')
            catalog = self.get_services()
            if not catalog.get('configured') or not catalog.get('items'):
                raise HTTPException(422, 'Каталог Airpay пуст или не настроен')
            if len(catalog['items']) > 2000:
                raise HTTPException(422, 'Каталог слишком большой для одного диагностического опроса')
            with conn.transaction():
                conn.execute('INSERT INTO supplier_hub_airpay.airpay_diagnostic_runs(id,created_by) VALUES (%s,%s)', (run_id, owner))
                for index, service in enumerate(catalog['items']):
                    conn.execute('INSERT INTO supplier_hub_airpay.airpay_diagnostic_items(run_id,position,service_id,title) VALUES (%s,%s,%s,%s)',
                                 (run_id, index, service['service_id'], service['title']))
            return self.store.read(owner, run_id, conn)

    def cancel(self, owner, run_id):
        # Завершаем только диагностический список; ни покупки, ни очередь оплаты не затрагиваются.
        with self.store.locked() as conn:
            self.store.read(owner, run_id, conn)
            conn.execute("UPDATE supplier_hub_airpay.airpay_diagnostic_runs SET state='cancelled',updated_at=now() WHERE id=%s AND state='active'", (run_id,))
            return self.store.read(owner, run_id, conn)

    def step(self, owner, run_id):
        # Один HTTP-вызов обрабатывает одну позицию; завершённые и оборванные check автоматически не повторяются.
        with self.store.locked() as conn:
            report = self.store.read(owner, run_id, conn)
            if report['run']['state'] != 'active':
                return report
            conn.execute("""UPDATE supplier_hub_airpay.airpay_diagnostic_items SET state='interrupted',finished_at=now(),
                report='{"message":"Предыдущий шаг прерван; check автоматически не повторялся"}'::jsonb
                WHERE run_id=%s AND state='running'""", (run_id,))
            if conn.execute("SELECT 1 FROM supplier_hub_airpay.airpay_diagnostic_items WHERE run_id=%s AND finished_at > now()-interval '1 second'", (run_id,)).fetchone():
                return self.store.read(owner, run_id, conn)
            row = conn.execute("SELECT * FROM supplier_hub_airpay.airpay_diagnostic_items WHERE run_id=%s AND state='pending' ORDER BY position LIMIT 1", (run_id,)).fetchone()
            if row:
                agent_id = secrets.randbelow(9223372036854775806) + 1
                conn.execute("UPDATE supplier_hub_airpay.airpay_diagnostic_items SET state='running',started_at=now(),agent_transaction_id=%s WHERE run_id=%s AND position=%s", (agent_id, run_id, row['position']))
                try:
                    service = self.get_service(row['service_id'])
                    payload, reason = diagnostic_payload(service)
                    if payload is None:
                        result = {'state': 'needs_input', 'message': reason}
                    else:
                        payload.update(agentTransactionId=agent_id, agentTransactionDate=datetime.now(timezone.utc).isoformat())
                        result = diagnostic_result(self.check(payload))
                    result['fixed_payment'] = service.get('fixed_payment')
                    result['fields'] = [{'name': field['name'], 'title': field['title'], 'required': field['required']} for field in service['inputs']]
                except HTTPException as exc:
                    result = {'state': 'transport_error', 'message': str(exc.detail)[:1000]}
                except Exception:
                    # Не публикуем исключения транспорта: в них могут оказаться заголовки или адреса с секретами.
                    result = {'state': 'transport_error', 'message': 'Не удалось завершить диагностический запрос'}
                conn.execute('UPDATE supplier_hub_airpay.airpay_diagnostic_items SET state=%s,report=%s,finished_at=now() WHERE run_id=%s AND position=%s',
                             (result['state'], Jsonb(result), run_id, row['position']))
            conn.execute("""UPDATE supplier_hub_airpay.airpay_diagnostic_runs SET updated_at=now(),state=CASE WHEN EXISTS
                (SELECT 1 FROM supplier_hub_airpay.airpay_diagnostic_items WHERE run_id=%s AND state='pending') THEN 'active' ELSE 'completed' END WHERE id=%s""", (run_id, run_id))
            return self.store.read(owner, run_id, conn)
