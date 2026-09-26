"""Airpay в Supplier Hub: отдельный процесс и схема, существующий Interhub не изменяется."""
from hmac import compare_digest
import json
import os
from urllib.parse import unquote

import psycopg
from fastapi import FastAPI, Header, HTTPException
from airpay_runtime.airpay_api import mount_airpay_routes
from airpay_runtime.airpay_jobs import AirpayJobStore
from airpay_runtime.airpay_repository import AirpayRepository
from airpay_runtime.airpay_service import build_airpay_service


def create_airpay_app(environ=None, connect=None):
    # Настройки Airpay изолированы; флаг Interhub не разрешает платёж другого поставщика.
    env = dict(os.environ if environ is None else environ)
    app = FastAPI(title='Supplier Hub · Airpay')
    database_url = env.get('DATABASE_URL', '')
    data_secret = env.get('SUPPLIER_HUB_DATA_SECRET', '')
    try:
        clients = json.loads(env.get('SUPPLIER_HUB_CLIENTS_JSON', '{}'))
    except ValueError:
        clients = {}
    if not isinstance(clients, dict):
        clients = {}
    enabled = env.get('SUPPLIER_HUB_PURCHASES_ENABLED', '').lower() in {'1', 'true', 'yes', 'on'}
    if not enabled:
        env['AIRPAY_PAYMENTS_ENABLED'] = 'false'
    service = build_airpay_service(env, local_ui=env.get('GAMESALES_LOCAL_UI', '').lower() in {'1', 'true', 'yes', 'on'})
    # Лимиты касаются только соединений Airpay и не меняют параметры PostgreSQL для Interhub.
    connection = connect or (lambda **kwargs: psycopg.connect(database_url, connect_timeout=5,
        application_name='supplier_hub_airpay', options='-c statement_timeout=15000 -c lock_timeout=3000', **kwargs))

    def user(x_hub_client: str = Header(default=''), x_hub_key: str = Header(default=''), x_airpay_owner: str = Header(default='')):
        # Клиент Hub аутентифицирует CRM; владельцы разных клиентов не имеют общих операций.
        expected = clients.get(x_hub_client, '')
        if not isinstance(expected, str) or len(expected) < 32 or not compare_digest(expected, x_hub_key):
            raise HTTPException(401, 'Invalid Supplier Hub credentials')
        owner = unquote(x_airpay_owner)
        if not owner or len(owner) > 128 or any(ord(char) < 32 for char in owner):
            raise HTTPException(422, 'Invalid Airpay owner')
        if len(data_secret) < 32:
            raise HTTPException(503, 'Airpay storage secret is not configured')
        # JSON-массив однозначно разделяет client и owner, включая двоеточия в именах.
        return {'role': 'owner', 'username': json.dumps([x_hub_client, owner], ensure_ascii=True, separators=(',', ':'))}

    mount_airpay_routes(app, get_current_user=user, service=service, get_secret=lambda: data_secret,
        repository=AirpayRepository(connection, code_secret=lambda: data_secret), job_store=AirpayJobStore(connection), execution_backend='hub')

    @app.get('/live')
    def live():
        # Liveness не запускает сетевых запросов к поставщику или БД.
        return {'status': 'ok', 'service': 'supplier-hub-airpay'}

    @app.get('/ready')
    def ready():
        # Готовность проверяет все новые таблицы до разрешения пользовательского сценария.
        if not database_url or len(data_secret) < 32 or not clients:
            raise HTTPException(503, 'Airpay Hub configuration is incomplete')
        try:
            with connection() as conn:
                tables = ('airpay_transactions', 'airpay_jobs', 'airpay_renewals', 'airpay_result_access', 'airpay_events', 'airpay_operator_actions', 'airpay_diagnostic_runs', 'airpay_diagnostic_items')
                for table in tables:
                    if conn.execute('SELECT to_regclass(%s)', ('supplier_hub_airpay.' + table,)).fetchone()[0] is None:
                        raise ValueError('migrations')
        except Exception:
            raise HTTPException(503, 'Airpay Hub database or migrations unavailable') from None
        return {'status': 'ready', 'payments_enabled': service.payments_enabled}

    return app


app = create_airpay_app()
