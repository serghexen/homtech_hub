"""Очередь действий Airpay: сеть только в worker, состояние и результат в PostgreSQL."""

from contextlib import asynccontextmanager
import asyncio
import logging
import threading
import uuid

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

SENSITIVE_ACTIONS = ('pay', 'voucher', 'reconcile')
INTERRUPTED = 'Исполнитель остановился во время действия. Сначала обновите сохранённую покупку; продолжение доступно только вручную. Ошибка задания не означает отказ оплаты.'


def public_job(row):
    # Подписи и исходные реквизиты задания не возвращаются в браузер.
    return {'job_id': str(row['id']), 'action': row['action'], 'state': row['state'],
            'transaction_id': str(row['transaction_id']), 'progress': row['progress'],
            'result': row['result'], 'error': row['error'], 'error_status': row['error_status'],
            'created_at': row.get('created_at'), 'updated_at': row.get('updated_at')}


def hide_codes(value):
    # Даже сохранённый результат worker не раскрывает ваучеры без отдельного действия и аудита.
    if isinstance(value, dict):
        return {key: hide_codes(item) for key, item in value.items() if key != 'pin_code'}
    if isinstance(value, list):
        return [hide_codes(item) for item in value]
    return value


class AirpayJobStore:
    def __init__(self, connect):
        # Соединения выдаются на один запрос; импорт модуля не подключается к БД.
        self.connect = connect

    def enqueue(self, owner, transaction_id, action, payload):
        # Уникальный активный ключ удерживает повтор HTTP на том же задании.
        with self.connect(autocommit=True, row_factory=dict_row) as conn:
            row = conn.execute('''INSERT INTO supplier_hub_airpay.airpay_jobs(id,created_by,transaction_id,action,payload)
                VALUES (%s,%s,%s,%s,%s) ON CONFLICT (created_by,transaction_id)
                WHERE state IN ('queued','running') DO UPDATE SET updated_at=supplier_hub_airpay.airpay_jobs.updated_at RETURNING *''',
                (uuid.uuid4(), owner, transaction_id, action, Jsonb(payload))).fetchone()
        if row['action'] != action or row['payload'] != payload:
            raise HTTPException(409, 'Другое действие этой покупки уже выполняется. Обновите результат.')
        return public_job(row)

    def get(self, owner, job_id):
        # Статус задания не вызывает поставщика и не раскрывает чужую покупку.
        with self.connect(autocommit=True, row_factory=dict_row) as conn:
            row = conn.execute('SELECT * FROM supplier_hub_airpay.airpay_jobs WHERE id=%s AND created_by=%s', (job_id, owner)).fetchone()
        if not row:
            raise HTTPException(404, 'Задание Airpay не найдено')
        return public_job(row)

    def active(self, owner, transaction_id):
        # История может найти работу после закрытия исходной вкладки.
        with self.connect(autocommit=True, row_factory=dict_row) as conn:
            row = conn.execute("SELECT * FROM supplier_hub_airpay.airpay_jobs WHERE created_by=%s AND transaction_id=%s AND state IN ('queued','running')", (owner, transaction_id)).fetchone()
        return public_job(row) if row else None

    def cancel(self, owner, job_id):
        # Отменяем только ещё не начатый запуск под тем же lock, что worker; оплату не отменяем.
        with self.connect(autocommit=True, row_factory=dict_row) as conn:
            row = conn.execute('SELECT * FROM supplier_hub_airpay.airpay_jobs WHERE id=%s AND created_by=%s', (job_id, owner)).fetchone()
            if not row:
                raise HTTPException(404, 'Задание Airpay не найдено')
            lock_name = 'airpay-job:' + str(job_id)
            if not conn.execute('SELECT pg_try_advisory_lock(hashtextextended(%s,0)) AS ok', (lock_name,)).fetchone()['ok']:
                raise HTTPException(409, 'Задание уже выполняется. Обновите сохранённую покупку.')
            try:
                row = conn.execute('SELECT * FROM supplier_hub_airpay.airpay_jobs WHERE id=%s AND created_by=%s', (job_id, owner)).fetchone()
                message = 'Запуск задания отменён владельцем. Сохранённые оплаты и результаты не изменены.'
                if row['state'] == 'failed' and row['error'] == message:
                    return public_job(row)
                if row['state'] != 'queued':
                    raise HTTPException(409, 'Можно отменить только ещё не начатое задание')
                row = conn.execute("UPDATE supplier_hub_airpay.airpay_jobs SET state='failed',error=%s,error_status=409,updated_at=now() WHERE id=%s RETURNING *",
                                   (message, job_id)).fetchone()
                logging.getLogger(__name__).info('Airpay queued job cancelled: id=%s', job_id)
                return public_job(row)
            finally:
                conn.execute('SELECT pg_advisory_unlock(hashtextextended(%s,0))', (lock_name,))

    def diagnostics(self, owner, payments_enabled):
        # Диагностика не захватывает блокировки и не меняет задания.
        from airpay_runtime.airpay_queue import queue_diagnostics
        return queue_diagnostics(self.connect, owner, payments_enabled)

    def latest(self, owner, transaction_id):
        # После сбоя история показывает последнее задание, не предлагая повтор автоматически.
        with self.connect(autocommit=True, row_factory=dict_row) as conn:
            row = conn.execute('SELECT * FROM supplier_hub_airpay.airpay_jobs WHERE created_by=%s AND transaction_id=%s ORDER BY created_at DESC,id DESC LIMIT 1',
                               (owner, transaction_id)).fetchone()
        return public_job(row) if row else None

    def candidates(self, conn, actions, owner):
        # Переходим за занятые первые 32 задания, чтобы чужие worker не останавливали всю очередь.
        cursor_date, cursor_id = None, None
        while True:
            rows = conn.execute("""SELECT id,created_at FROM supplier_hub_airpay.airpay_jobs
                WHERE state IN ('queued','running')
                  AND (action=ANY(%s) OR (state='running' AND action=ANY(%s)))
                  AND (%s::text IS NULL OR created_by=%s)
                  AND (%s::timestamptz IS NULL OR (created_at,id)>(%s,%s::uuid))
                ORDER BY created_at,id LIMIT 32""",
                (list(actions), list(SENSITIVE_ACTIONS), owner, owner, cursor_date, cursor_date, cursor_id)).fetchall()
            yield from rows
            if len(rows) < 32:
                return
            cursor_date, cursor_id = rows[-1]['created_at'], rows[-1]['id']

    def run_one(self, actions, execute, *, owner=None):
        # Сессионный lock переживает commits и освобождается PostgreSQL при падении процесса.
        with self.connect(autocommit=True, row_factory=dict_row) as conn:
            for item in self.candidates(conn, actions, owner):
                lock_name = 'airpay-job:' + str(item['id'])
                acquired = conn.execute('SELECT pg_try_advisory_lock(hashtextextended(%s,0)) AS ok', (lock_name,)).fetchone()['ok']
                if not acquired:
                    continue
                try:
                    row = conn.execute("SELECT * FROM supplier_hub_airpay.airpay_jobs WHERE id=%s AND state IN ('queued','running')", (item['id'],)).fetchone()
                    if not row:
                        continue
                    if row['state'] == 'running' and row['action'] in SENSITIVE_ACTIONS:
                        # Свободный lock доказывает потерю исполнителя, но не доказывает отсутствие оплаты.
                        conn.execute("UPDATE supplier_hub_airpay.airpay_jobs SET state='failed',error=%s,error_status=409,updated_at=now() WHERE id=%s",
                                     (INTERRUPTED, row['id']))
                        logging.getLogger(__name__).warning('Airpay interrupted job stopped: id=%s action=%s', row['id'], row['action'])
                        return True
                    if row['action'] not in actions:
                        continue
                    conn.execute("UPDATE supplier_hub_airpay.airpay_jobs SET state='running',updated_at=now() WHERE id=%s", (row['id'],))
                    def progress(value):
                        # Прогресс фиксируется после каждой позиции и читается независимо от сети поставщика.
                        conn.execute('UPDATE supplier_hub_airpay.airpay_jobs SET progress=%s,updated_at=now() WHERE id=%s', (value, row['id']))
                    try:
                        result = execute(row, progress)
                    except HTTPException as exc:
                        conn.execute("UPDATE supplier_hub_airpay.airpay_jobs SET state='failed',error=%s,error_status=%s,updated_at=now() WHERE id=%s", (str(exc.detail)[:2000], exc.status_code, row['id']))
                    except Exception:
                        # Не выводим реквизиты исключения; journal остаётся источником истины после сбоя.
                        logging.getLogger(__name__).error('Airpay job interrupted; inspect saved transaction state')
                        conn.execute("UPDATE supplier_hub_airpay.airpay_jobs SET state='failed',error='Запрос прерван. Обновите сохранённый результат покупки.',error_status=503,updated_at=now() WHERE id=%s", (row['id'],))
                    else:
                        conn.execute("UPDATE supplier_hub_airpay.airpay_jobs SET state='succeeded',result=%s,updated_at=now() WHERE id=%s", (Jsonb(jsonable_encoder(hide_codes(result))), row['id']))
                    return True
                finally:
                    conn.execute('SELECT pg_advisory_unlock(hashtextextended(%s,0))', (lock_name,))
        return False


class AirpayJobs:
    def __init__(self, store, preparation, purchase, batch):
        # Очередь не запускает автоматическую сверку pay: только сохранённые действия пользователя.
        self.store, self.preparation, self.purchase, self.batch = store, preparation, purchase, batch
        self.stop_event = threading.Event()

    def execute(self, job, progress):
        # После сбоя используем старые строки и ID; processing требует отдельной ручной сверки.
        callback = progress
        def progress(value):
            # После сохранения позиции остановка процесса запрещает начинать следующую.
            callback(value)
            if self.stop_event.is_set():
                raise HTTPException(409, INTERRUPTED)
        if self.stop_event.is_set():
            raise HTTPException(409, INTERRUPTED)
        owner, transaction_id, action = job['created_by'], job['transaction_id'], job['action']
        if action == 'check':
            return self.batch.check(owner, job['payload']['token'], progress=progress)
        if action == 'renew':
            return self.batch.renew(owner, transaction_id)
        self.purchase.require_enabled()
        if action == 'pay':
            return self.batch.pay(owner, transaction_id, job['payload']['confirmed_amount'], progress=progress)
        if action == 'voucher':
            return self.batch.vouchers(owner, transaction_id, progress=progress)
        if action == 'reconcile':
            return self.purchase.reconcile(owner, transaction_id)
        raise HTTPException(422, 'Неизвестное действие Airpay')

    def run_one(self):
        # Локальный worker выполняет только явно запрошенные check/переоценку, платёжные задания не забирает.
        actions = ('check', 'renew', 'pay', 'voucher', 'reconcile') if self.purchase.service.payments_enabled else ('check', 'renew')
        return self.store.run_one(actions, self.execute)

    def install(self, app):
        # Собственный lifecycle не меняет фоновые задачи Interhub и останавливается вместе с API.
        previous = app.router.lifespan_context
        @asynccontextmanager
        async def lifespan(application):
            # База должна быть мигрирована до запуска; здесь нет DDL и автоматических покупок.
            async with previous(application):
                stop = self.stop_event
                stop.clear()
                def run():
                    # Небольшая пауза ограничивает опрос пустой очереди и ошибки инфраструктуры.
                    while not stop.wait(1):
                        try:
                            self.run_one()
                        except Exception:
                            logging.getLogger(__name__).error('Airpay queue unavailable; check migrations and database')
                            stop.wait(5)
                thread = threading.Thread(target=run, name='airpay-jobs', daemon=True)
                thread.start()
                try:
                    yield
                finally:
                    stop.set()
                    await asyncio.to_thread(thread.join, 5)
                    if thread.is_alive():
                        logging.getLogger(__name__).warning('Airpay worker shutdown timed out; saved running jobs will require recovery')
        app.router.lifespan_context = lifespan
