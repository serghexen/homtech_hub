"""Долговечный журнал Airpay без DDL и без подключения при импорте."""

from contextlib import contextmanager
from datetime import datetime, timezone
import uuid
from hashlib import sha256

from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg.errors import UniqueViolation
from airpay_runtime.airpay_resolution import resolution_changes, resolution_fingerprint


JSON_FIELDS = {'request_payload', 'service_snapshot', 'check_response', 'check_result', 'pay_request', 'pay_response', 'voucher_response'}
UPDATE_FIELDS = JSON_FIELDS | {'state', 'amount', 'currency', 'provider_transaction_id', 'provider_message', 'pin_code',
                               'check_attempts', 'pay_attempts', 'voucher_attempts', 'next_attempt_at',
                               'requires_attention', 'attention_reason', 'resolution_request_id'}


def stored_row(row):
    # Для работы координатора достаточно признака кода; расшифровка доступна только через reveal с аудитом.
    if row and row.get('pin_ciphertext'):
        row = {**row, 'pin_code': '[stored]'}
    return row


class AirpayRepository:
    def __init__(self, connect, code_secret=lambda: ''):
        # Фабрика создаёт отдельное соединение только для запрошенной операции.
        self.connect = connect
        self.code_secret = code_secret

    def create(self, owner, key, request, service, kind, expires_at):
        # Один ключ подготовки возвращает прежний снимок даже после потери HTTP-ответа.
        with self.connect(autocommit=True, row_factory=dict_row) as conn:
            conn.execute('''INSERT INTO supplier_hub_airpay.airpay_transactions
                (agent_transaction_id, preparation_key, created_by, service_id, service_title, account,
                 purchase_kind, request_payload, service_snapshot, expires_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (created_by, preparation_key) DO NOTHING''',
                (request['agentTransactionId'], key, owner, service['service_id'], service['title'], request['account'],
                 kind, Jsonb(request), Jsonb(service), datetime.fromtimestamp(expires_at, timezone.utc)))
            row = conn.execute('SELECT * FROM supplier_hub_airpay.airpay_transactions WHERE created_by=%s AND preparation_key=%s', (owner, key)).fetchone()
        original = {k: v for k, v in row['request_payload'].items() if k not in {'agentTransactionId', 'agentTransactionDate'}}
        incoming = {k: v for k, v in request.items() if k not in {'agentTransactionId', 'agentTransactionDate'}}
        if original != incoming or row['purchase_kind'] != kind or row['service_snapshot'].get('_airpay_batch') != service.get('_airpay_batch'):
            raise HTTPException(409, 'Этот ключ подготовки уже использован с другими реквизитами')
        return stored_row(row)

    @contextmanager
    def locked(self, owner, transaction_id):
        # Сессионная блокировка удерживается через сеть; каждое состояние коммитится ДО внешнего вызова.
        with self.connect(autocommit=True, row_factory=dict_row) as conn:
            acquired = conn.execute('SELECT pg_try_advisory_lock(%s) AS acquired', (int(transaction_id),)).fetchone()['acquired']
            if not acquired:
                raise HTTPException(409, 'Эта операция Airpay уже выполняется. Обновите историю позже.')
            try:
                row = conn.execute('SELECT * FROM supplier_hub_airpay.airpay_transactions WHERE agent_transaction_id=%s AND created_by=%s',
                                   (transaction_id, owner)).fetchone()
                if not row:
                    raise HTTPException(404, 'Операция Airpay не найдена')
                yield AirpayRecord(conn, stored_row(row), self.code_secret)
            finally:
                conn.execute('SELECT pg_advisory_unlock(%s)', (int(transaction_id),))

    def batch_rows(self, owner, root):
        # Уникальный индекс ключей подготовки позволяет восстановить пачку без новой схемы БД.
        from airpay_runtime.airpay_batch import batch_metadata, preparation_keys
        meta = batch_metadata(root)
        if meta.get('index') != 0 or not 1 <= meta.get('quantity', 0) <= 20:
            raise HTTPException(404, 'Пачка Airpay не найдена')
        keys = preparation_keys(meta['key'], meta['quantity'])
        with self.connect(autocommit=True, row_factory=dict_row) as conn:
            rows = conn.execute('SELECT * FROM supplier_hub_airpay.airpay_transactions WHERE created_by=%s AND preparation_key = ANY(%s::uuid[])',
                                (owner, keys)).fetchall()
        by_key = {str(row['preparation_key']): row for row in rows}
        if len(by_key) != len(keys):
            raise HTTPException(409, 'Подготовка количества не завершена. Повторите подготовку.')
        result = [stored_row(by_key[key]) for key in keys]
        for index, row in enumerate(result):
            if batch_metadata(row) != {**meta, 'index': index} or row['purchase_kind'] != 'voucher':
                raise HTTPException(409, 'Состав покупки не совпадает с подготовкой')
        return result

    @contextmanager
    def batch_locked(self, owner, transaction_id):
        # Любая позиция ведёт к корню пачки; отрицательный lock не пересекается с отдельными платежами.
        with self.connect(autocommit=True, row_factory=dict_row) as conn:
            root = conn.execute('SELECT * FROM supplier_hub_airpay.airpay_transactions WHERE agent_transaction_id=%s AND created_by=%s',
                                (transaction_id, owner)).fetchone()
            if not root or not root['service_snapshot'].get('_airpay_batch'):
                raise HTTPException(404, 'Пачка Airpay не найдена')
            key = root['service_snapshot']['_airpay_batch']['key']
            root = conn.execute('SELECT * FROM supplier_hub_airpay.airpay_transactions WHERE preparation_key=%s AND created_by=%s',
                                (key, owner)).fetchone()
            if not root:
                raise HTTPException(404, 'Пачка Airpay не найдена')
            lock_id = -int(root['agent_transaction_id'])
            acquired = conn.execute('SELECT pg_try_advisory_lock(%s) AS acquired', (lock_id,)).fetchone()['acquired']
            if not acquired:
                raise HTTPException(409, 'Покупка этого количества уже выполняется. Обновите результат позже.')
            try:
                yield self.batch_rows(owner, root)
            finally:
                conn.execute('SELECT pg_advisory_unlock(%s)', (lock_id,))

    def history(self, owner, limit, offset, filters=None):
        # Поиск и выгрузка используют один фильтр с обязательной изоляцией владельца.
        from airpay_runtime.airpay_history import HistoryFilters, history_where
        where, params = history_where(owner, filters or HistoryFilters())
        with self.connect(autocommit=True, row_factory=dict_row) as conn:
            return [stored_row(row) for row in conn.execute(f'SELECT * FROM supplier_hub_airpay.airpay_transactions WHERE {where} ORDER BY created_at DESC, agent_transaction_id DESC LIMIT %s OFFSET %s',
                                (*params, limit, offset)).fetchall()]

    def cutover_status(self):
        # Проверяем весь старый журнал: другой владелец тоже может ожидать оплату или код.
        with self.connect(row_factory=dict_row) as conn:
            row = conn.execute('''SELECT
                (SELECT count(*) FROM supplier_hub_airpay.airpay_transactions WHERE state='processing') AS pending_payments,
                (SELECT count(*) FROM supplier_hub_airpay.airpay_transactions
                 WHERE state='paid' AND purchase_kind='voucher'
                   AND pin_ciphertext IS NULL AND COALESCE(btrim(pin_code),'')='') AS missing_vouchers,
                (SELECT count(*) FROM supplier_hub_airpay.airpay_jobs WHERE state IN ('queued','running')) AS active_jobs
                ''').fetchone()
        counts = {key: int(row[key]) for key in ('pending_payments', 'missing_vouchers', 'active_jobs')}
        return {**counts, 'ready': not any(counts.values())}

    def read(self, owner, transaction_id):
        # Чтение прогресса не ждёт сетевой запрос, удерживающий платёжную блокировку.
        with self.connect(autocommit=True, row_factory=dict_row) as conn:
            row = conn.execute('SELECT * FROM supplier_hub_airpay.airpay_transactions WHERE agent_transaction_id=%s AND created_by=%s', (transaction_id, owner)).fetchone()
        if not row:
            raise HTTPException(404, 'Операция Airpay не найдена')
        return stored_row(row)

    def batch_root(self, owner, transaction_id):
        # Чтение любой позиции истории находит корень без блокировки выполняющегося pay.
        row = self.read(owner, transaction_id)
        key = (row['service_snapshot'].get('_airpay_batch') or {}).get('key')
        if not key:
            raise HTTPException(404, 'Пачка Airpay не найдена')
        with self.connect(autocommit=True, row_factory=dict_row) as conn:
            root = conn.execute('SELECT * FROM supplier_hub_airpay.airpay_transactions WHERE preparation_key=%s AND created_by=%s', (key, owner)).fetchone()
        if not root:
            raise HTTPException(404, 'Пачка Airpay не найдена')
        return stored_row(root)

    def renew(self, owner, rows):
        # Старая неоплаченная часть блокируется атомарно до создания замены, без изменения оплаченных строк.
        with self.connect(row_factory=dict_row) as conn:
            existing = conn.execute('SELECT * FROM supplier_hub_airpay.airpay_renewals WHERE source_id=%s AND created_by=%s', (rows[0]['agent_transaction_id'], owner)).fetchone()
            if existing:
                return existing
            if any(row['state'] == 'processing' for row in rows):
                raise HTTPException(409, 'Сначала уточните незавершённую оплату; остаток пока неизвестен.')
            pending = [row for row in rows if row['state'] != 'paid']
            if not pending:
                raise HTTPException(409, 'Все позиции уже оплачены')
            key = uuid.uuid4()
            result = conn.execute('''INSERT INTO supplier_hub_airpay.airpay_renewals(source_id,preparation_key,created_by,quantity,source_transaction_id)
                VALUES (%s,%s,%s,%s,%s) RETURNING *''', (rows[0]['agent_transaction_id'], key, owner, len(pending), pending[0]['agent_transaction_id'])).fetchone()
            conn.execute('UPDATE supplier_hub_airpay.airpay_transactions SET replacement_key=%s,updated_at=now() WHERE agent_transaction_id=ANY(%s)', (key, [row['agent_transaction_id'] for row in pending]))
            return result

    def finish_renewal(self, owner, source_id, replacement_id):
        # Ссылка переживает потерю ответа; повтор возвращает ту же подготовку оставшихся ключей.
        with self.connect(autocommit=True) as conn:
            conn.execute('UPDATE supplier_hub_airpay.airpay_renewals SET replacement_id=%s WHERE source_id=%s AND created_by=%s', (replacement_id, source_id, owner))

    def replacement(self, owner, source_id):
        # История показывает цепочку продолжений вместо предложения снова купить всё количество.
        with self.connect(autocommit=True, row_factory=dict_row) as conn:
            row = conn.execute('SELECT replacement_id FROM supplier_hub_airpay.airpay_renewals WHERE source_id=%s AND created_by=%s', (source_id, owner)).fetchone()
        return str(row['replacement_id']) if row and row['replacement_id'] else None

    def reveal(self, owner, transaction_id):
        # Код раскрывается только по явному запросу; факт просмотра сохраняется в той же транзакции.
        with self.connect(row_factory=dict_row) as conn:
            row = conn.execute("SELECT CASE WHEN pin_ciphertext IS NOT NULL THEN pgp_sym_decrypt(pin_ciphertext,%s) ELSE pin_code END AS pin_code FROM supplier_hub_airpay.airpay_transactions WHERE agent_transaction_id=%s AND created_by=%s AND state='paid'", (self.code_secret(), transaction_id, owner)).fetchone()
            if not row or not row['pin_code']:
                raise HTTPException(404, 'Сохранённый код не найден')
            conn.execute('INSERT INTO supplier_hub_airpay.airpay_result_access(transaction_id,viewed_by) VALUES (%s,%s)', (transaction_id, owner))
            return {'agent_transaction_id': str(transaction_id), 'value': row['pin_code']}

    def events(self, owner, transaction_id, before=None, limit=50):
        # Сначала проверяем владельца, затем читаем только безопасный журнал по убыванию ID.
        self.read(owner, transaction_id)
        with self.connect(row_factory=dict_row) as conn:
            rows = conn.execute('''SELECT e.id::text,e.event_type,e.state_before,e.state_after,e.actor,e.details,e.created_at,
                a.decision,a.evidence FROM supplier_hub_airpay.airpay_events e
                LEFT JOIN supplier_hub_airpay.airpay_operator_actions a ON a.transaction_id=e.transaction_id
                  AND a.request_id::text=e.details->>'resolution_request_id'
                WHERE e.transaction_id=%s AND (%s::bigint IS NULL OR e.id<%s)
                ORDER BY e.id DESC LIMIT %s''', (transaction_id,before,before,limit+1)).fetchall()
        return {'items': rows[:limit], 'next_cursor': rows[limit-1]['id'] if len(rows)>limit else None}

    def resolve(self, owner, transaction_id, payload):
        # Решение, зашифрованный код и событие фиксируются атомарно под тем же lock, что pay.
        digest = resolution_fingerprint(payload)
        with self.locked(owner, transaction_id) as record:
            with record.conn.transaction():
                existing = record.conn.execute('''SELECT request_hash FROM supplier_hub_airpay.airpay_operator_actions
                    WHERE transaction_id=%s AND request_id=%s''', (transaction_id,payload['request_id'])).fetchone()
                if existing:
                    if existing['request_hash'] != digest:
                        raise HTTPException(409, 'Ключ решения уже использован с другим содержимым.')
                    return record.row
                changes = resolution_changes(record.row, payload)
                if changes.get('pin_code') and not self.code_secret():
                    raise HTTPException(503, 'Не настроен ключ шифрования результата Airpay.')
                record.conn.execute('''INSERT INTO supplier_hub_airpay.airpay_operator_actions
                    (transaction_id,request_id,request_hash,decision,evidence,decided_by)
                    VALUES (%s,%s,%s,%s,%s,%s)''',
                    (transaction_id,payload['request_id'],digest,payload['decision'],payload['evidence'].strip(),owner))
                try:
                    record.update(**changes)
                except UniqueViolation:
                    raise HTTPException(409, 'Такой код уже сохранён для другой операции. Решение не применено.') from None
            return record.row


class AirpayRecord:
    def __init__(self, conn, row, code_secret=lambda: ''):
        # Объект живёт только пока удерживается блокировка конкретной операции.
        self.conn, self.row = conn, row
        self.code_secret = code_secret

    def update(self, **changes):
        # Разрешённые поля заданы кодом, а значения всегда передаются параметрами SQL.
        if not changes or set(changes) - UPDATE_FIELDS:
            raise ValueError('Unsupported Airpay journal update')
        assignments, values = [], []
        for key, value in changes.items():
            if key == 'pin_code' and value and self.code_secret():
                # В БД остаётся только шифротекст и хэш для защиты от повторной выдачи того же кода.
                assignments.extend(["pin_code=''", 'pin_ciphertext=pgp_sym_encrypt(%s,%s)', 'result_hash=%s'])
                values.extend([value, self.code_secret(), sha256(value.encode()).hexdigest()])
                continue
            if key == 'voucher_response' and isinstance(value, dict):
                # Сырой JSON не должен обходить шифрование дубликатом pinCode.
                value = {**value, 'displays': {k: v for k, v in (value.get('displays') or {}).items() if k != 'pinCode'}}
            assignments.append(f'{key}=%s')
            values.append(Jsonb(value) if key in JSON_FIELDS else value)
        self.row = stored_row(self.conn.execute('UPDATE supplier_hub_airpay.airpay_transactions SET ' + ', '.join(assignments)
                                    + ', updated_at=now() WHERE agent_transaction_id=%s RETURNING *',
                                    (*values, self.row['agent_transaction_id'])).fetchone())
        return self.row
