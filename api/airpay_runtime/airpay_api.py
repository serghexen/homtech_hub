"""Чтение справочников и подготовка Airpay до отправки платежа."""

from fastapi import Depends, HTTPException, Query, Path
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ConfigDict, SecretStr, AwareDatetime
from typing import Annotated
from typing import Literal
from uuid import UUID
from airpay_runtime.airpay_batch import AirpayBatch, BATCH_META
from airpay_runtime.airpay_preparation import AirpayPreparation
from airpay_runtime.airpay_purchase import AirpayPurchase, public_transaction
from airpay_runtime.airpay_jobs import AirpayJobs, hide_codes
from airpay_runtime.airpay_contract import CONTRACT_HEADER, CONTRACT_VERSION, describe_contract

from airpay_runtime.airpay_history import read_history_filters, export_history

AirpayTransactionId = Annotated[int, Path(ge=1, le=9223372036854775807)]


class AirpayBalanceOut(BaseModel):
    configured: bool
    balance: float | None = None
    overdraft: float | None = None
    currency: str = ''


class AirpayFieldOut(BaseModel):
    name: str
    title: str = ''
    required: bool = False
    regexp: str = ''


class AirpayServiceOut(BaseModel):
    service_id: str
    title: str
    type: int
    group: str = ''
    country: str = ''
    fixed_payment: bool | None = None
    inputs: list[AirpayFieldOut] = Field(default_factory=list)
    displays: list[AirpayFieldOut] = Field(default_factory=list)


class AirpayServicesOut(BaseModel):
    configured: bool
    items: list[AirpayServiceOut] = Field(default_factory=list)
    total: int = 0


class AirpayPrepareIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    service_id: str = Field(min_length=1, max_length=128)
    fields: dict[Annotated[str, Field(max_length=128)], Annotated[str, Field(max_length=512)]] = Field(default_factory=dict, max_length=64)
    amount_to: str | None = Field(default=None, max_length=32)
    amount_from: str | None = Field(default=None, max_length=32)
    purchase_kind: Literal['voucher', 'topup'] | None = None
    preparation_key: UUID | None = None
    quantity: int = Field(default=1, ge=1, le=20, strict=True)


class AirpayCheckIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    preparation_token: str = Field(min_length=1, max_length=100000)


class AirpayPayIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    confirmed_amount: str = Field(min_length=1, max_length=32)


class AirpayResolutionIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: UUID
    decision: Literal['confirm_failed', 'record_success']
    expected_updated_at: AwareDatetime
    verified: bool = Field(strict=True)
    evidence: str = Field(min_length=10, max_length=1000)
    provider_transaction_id: str = Field(default='', max_length=128)
    code: SecretStr = SecretStr('')


def mount_airpay_routes(app, *, get_current_user, service, get_secret=lambda: '', offline=False, repository=None, job_store=None, execution_backend='crm'):
    # Используем авторизацию CRM, как в существующем endpoint баланса Interhub.
    preparation = AirpayPreparation(service, get_secret, repository)
    purchase = AirpayPurchase(service, repository, preparation)
    batch = AirpayBatch(service, repository, preparation, purchase)
    jobs = AirpayJobs(job_store, preparation, purchase, batch) if job_store else None
    if jobs:
        jobs.install(app)

    @app.middleware('http')
    async def contract_boundary(request, call_next):
        # Hub отклоняет несовместимого клиента до обработчика и любого действия поставщика.
        if not request.url.path.startswith('/integrations/airpay/'):
            return await call_next(request)
        version = request.headers.get(CONTRACT_HEADER)
        if (version is not None and version != CONTRACT_VERSION) or (execution_backend == 'hub' and request.method == 'POST' and version != CONTRACT_VERSION):
            return JSONResponse({'detail': 'Несовместимая версия API Airpay'}, status_code=409,
                                headers={CONTRACT_HEADER: CONTRACT_VERSION})
        response = await call_next(request)
        response.headers[CONTRACT_HEADER] = CONTRACT_VERSION
        return response

    def submit(owner, transaction_id, action, payload):
        # Состав и владелец проверяются до постановки; HTTP никогда не ждёт всю пачку.
        with repository.batch_locked(owner, transaction_id) as rows:
            if action in {'pay', 'voucher'}:
                purchase.require_enabled()
            if action == 'pay' and any(row.get('replacement_key') for row in rows):
                raise HTTPException(409, 'Покупка заменена новой подготовкой остатка')
            return {'job': job_store.enqueue(owner, rows[0]['agent_transaction_id'], action, payload)}

    def require_owner(user=Depends(get_current_user)):
        # История и ваучерные коды доступны только владельцу, включая чтение в offline-режиме.
        role = user.get('role') if isinstance(user, dict) else user.role
        if role != 'owner':
            raise HTTPException(403, 'Проверять покупки Airpay может только владелец')
        return user.get('username') if isinstance(user, dict) else user.username

    def require_payment_owner(owner=Depends(require_owner)):
        # Оплата и выдача кода запрещены на staging; предварительный check доступен владельцу отдельно.
        if offline:
            raise HTTPException(403, 'На staging оплата и получение ваучеров отключены')
        return owner

    def require_journal():
        # Без подключённого журнала оплату не отправляем и историю не имитируем пустой.
        if repository is None:
            raise HTTPException(503, 'Журнал операций Airpay не подключён')

    @app.get('/integrations/airpay/contract')
    def airpay_contract(user=Depends(get_current_user)):
        # Согласование версии не обращается ни к поставщику, ни к журналу покупок.
        return describe_contract(execution_backend, service.payments_enabled)

    @app.get('/integrations/airpay/cutover')
    def airpay_cutover(owner=Depends(require_owner)):
        # Старый исполнитель позволяет проверить незавершённые операции ещё до смены настройки CRM.
        require_journal()
        return repository.cutover_status()

    @app.get('/integrations/airpay/balance', response_model=AirpayBalanceOut)
    def get_airpay_balance(user=Depends(get_current_user)):
        # Возвращаем только суммы; имя агента и реквизиты Basic остаются на сервере.
        return service.get_balance()

    @app.get('/integrations/airpay/services', response_model=AirpayServicesOut)
    def get_airpay_services(user=Depends(get_current_user)):
        # Внутренний GET читает справочник; внешний POST Airpay не создаёт платёж.
        return service.get_services()

    @app.get('/integrations/airpay/service', response_model=AirpayServiceOut)
    def get_airpay_service(service_id: str = Query(min_length=1, max_length=128), user=Depends(get_current_user)):
        # Точная схема выбранной услуги нужна для актуальной формы ввода.
        return service.get_service(service_id)

    @app.post('/integrations/airpay/prepare')
    def prepare_airpay(payload: AirpayPrepareIn, owner=Depends(require_owner)):
        # Подготовка доступна владельцу независимо от разрешения платных операций.
        return preparation.prepare(owner, payload.service_id, payload.fields, payload.amount_to, payload.amount_from,
                                   purchase_kind=payload.purchase_kind, preparation_key=str(payload.preparation_key) if payload.preparation_key else None, quantity=payload.quantity)

    @app.post('/integrations/airpay/check')
    def check_airpay(payload: AirpayCheckIn, owner=Depends(require_owner)):
        # Проверяем реквизиты и сохраняем результат, не вызывая pay или выдачу кода.
        draft = preparation.read_draft(owner, payload.preparation_token)
        if draft['service'].get(BATCH_META):
            require_journal()
            if jobs:
                return submit(owner, draft['request']['agentTransactionId'], 'check', {'token': payload.preparation_token})
            return hide_codes(batch.check(owner, payload.preparation_token))
        return hide_codes(purchase.check(owner, payload.preparation_token) if repository is not None else preparation.check(owner, payload.preparation_token))

    @app.get('/integrations/airpay/transactions')
    def airpay_history(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0), owner=Depends(require_owner), filters=Depends(read_history_filters)):
        # Постраничная история позволяет восстановить результат после закрытия окна покупки.
        require_journal()
        return hide_codes({'items': [public_transaction(row, service.payments_enabled) for row in repository.history(owner, limit, offset, filters)]})

    @app.get('/integrations/airpay/transactions/export')
    def airpay_history_export(owner=Depends(require_owner), filters=Depends(read_history_filters)):
        # Excel содержит только сохранённые сведения текущего владельца, без раскрытия кодов.
        require_journal()
        return export_history(repository, owner, filters)

    @app.get('/integrations/airpay/transactions/{transaction_id}')
    def airpay_transaction(transaction_id: AirpayTransactionId, owner=Depends(require_owner)):
        # Чтение сохранённого результата не вызывает ни pay, ни получение кода у поставщика.
        require_journal()
        return hide_codes(public_transaction(repository.read(owner, transaction_id), service.payments_enabled))

    @app.post('/integrations/airpay/transactions/{transaction_id}/result')
    def airpay_result(transaction_id: AirpayTransactionId, owner=Depends(require_owner)):
        # Только чтение уже сохранённого кода с аудитом просмотра; никакого обращения к поставщику.
        require_journal()
        return repository.reveal(owner, transaction_id)

    @app.get('/integrations/airpay/transactions/{transaction_id}/events')
    def airpay_events(transaction_id: AirpayTransactionId, before: int | None = Query(None, ge=1, le=9223372036854775807), owner=Depends(require_owner)):
        # Журнал читается из БД без запросов статуса или оплаты поставщику.
        require_journal()
        return repository.events(owner, transaction_id, before)

    @app.post('/integrations/airpay/transactions/{transaction_id}/resolve')
    def airpay_resolve(transaction_id: AirpayTransactionId, payload: AirpayResolutionIn, owner=Depends(require_owner)):
        # Вызов не получает транспорт: меняем только проверенный журнал с аудитом решения.
        require_journal()
        data = payload.model_dump(exclude={'code'}, mode='json')
        data['code'] = payload.code.get_secret_value()
        return public_transaction(repository.resolve(owner, transaction_id, data), service.payments_enabled)

    @app.post('/integrations/airpay/transactions/{transaction_id}/pay')
    def airpay_pay(transaction_id: AirpayTransactionId, payload: AirpayPayIn, owner=Depends(require_payment_owner)):
        # Пользователь подтверждает сохранённую цену; произвольные реквизиты оплаты не принимаем.
        require_journal()
        return hide_codes(purchase.pay(owner, transaction_id, payload.confirmed_amount))

    @app.post('/integrations/airpay/transactions/{transaction_id}/reconcile')
    def airpay_reconcile(transaction_id: AirpayTransactionId, owner=Depends(require_payment_owner)):
        # Повторяется только сохранённый pay уже начатой операции.
        require_journal()
        return hide_codes(purchase.reconcile(owner, transaction_id))

    @app.post('/integrations/airpay/transactions/{transaction_id}/voucher')
    def airpay_voucher(transaction_id: AirpayTransactionId, owner=Depends(require_payment_owner)):
        # Отдельное получение ваучера не может инициировать новую оплату.
        require_journal()
        return hide_codes(purchase.voucher(owner, transaction_id))

    @app.get('/integrations/airpay/batches/{transaction_id}')
    def airpay_batch(transaction_id: AirpayTransactionId, owner=Depends(require_owner)):
        # Восстанавливаем все позиции без сетевых запросов к поставщику.
        require_journal()
        value = batch.read(owner, transaction_id)
        if jobs:
            value['job'] = job_store.active(owner, int(value['batch_id']))
            value['last_job'] = job_store.latest(owner, int(value['batch_id'])) if not value['job'] else None
        return hide_codes(value)

    @app.post('/integrations/airpay/batches/{transaction_id}/pay')
    def airpay_batch_pay(transaction_id: AirpayTransactionId, payload: AirpayPayIn, owner=Depends(require_payment_owner)):
        # Одна подтверждённая общая сумма оплачивается отдельными последовательными операциями.
        require_journal()
        if jobs:
            return submit(owner, transaction_id, 'pay', {'confirmed_amount': payload.confirmed_amount})
        return hide_codes(batch.pay(owner, transaction_id, payload.confirmed_amount))

    @app.post('/integrations/airpay/batches/{transaction_id}/vouchers')
    def airpay_batch_vouchers(transaction_id: AirpayTransactionId, owner=Depends(require_payment_owner)):
        # Выдача сохранённых или уже оплаченных кодов не начинает новую покупку.
        require_journal()
        if jobs:
            return submit(owner, transaction_id, 'voucher', {})
        return hide_codes(batch.vouchers(owner, transaction_id))

    @app.post('/integrations/airpay/batches/{transaction_id}/renew')
    def airpay_batch_renew(transaction_id: AirpayTransactionId, owner=Depends(require_owner)):
        # Переоценка доступна без разрешения pay; она не покупает и не выдаёт коды.
        require_journal()
        if jobs:
            return submit(owner, transaction_id, 'renew', {})
        return hide_codes(batch.renew(owner, transaction_id))

    @app.post('/integrations/airpay/batches/{transaction_id}/check')
    def airpay_batch_check(transaction_id: AirpayTransactionId, owner=Depends(require_owner)):
        # Повтор из истории использует прежние реквизиты, а не создаёт новую подготовку.
        require_journal()
        token = batch.saved_token(owner, transaction_id)
        if jobs:
            return submit(owner, transaction_id, 'check', {'token': token})
        return hide_codes(batch.check(owner, token))

    @app.get('/integrations/airpay/queue')
    def airpay_queue(owner=Depends(require_owner)):
        # Только сохранённая очередь владельца; чтение не инициирует восстановление или оплату.
        if not jobs:
            raise HTTPException(503, 'Очередь Airpay не настроена')
        return job_store.diagnostics(owner, service.payments_enabled)

    @app.post('/integrations/airpay/jobs/{job_id}/cancel')
    def airpay_job_cancel(job_id: UUID, owner=Depends(require_owner)):
        # Остановка очереди не вызывает поставщика и не меняет финансовый результат покупки.
        if not jobs:
            raise HTTPException(503, 'Очередь Airpay не настроена')
        return {'job': job_store.cancel(owner, job_id)}

    @app.get('/integrations/airpay/jobs/{job_id}')
    def airpay_job(job_id: UUID, owner=Depends(require_owner)):
        # Опрос прогресса идёт только в БД; открытие истории не повторяет платёж.
        if not jobs:
            raise HTTPException(503, 'Очередь Airpay не настроена')
        return {'job': job_store.get(owner, job_id)}
