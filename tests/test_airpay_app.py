"""Контракт Airpay Hub без сети и реальных покупок."""
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient
from hub.airpay_app import create_airpay_app


class AirpayAppTests(unittest.TestCase):
    def setUp(self):
        # Все внешние операции заменяются объектами памяти, credentials только тестовые.
        self.env = {'DATABASE_URL': 'unused', 'SUPPLIER_HUB_DATA_SECRET': 'd' * 32,
            'SUPPLIER_HUB_CLIENTS_JSON': json.dumps({'crm': 'k' * 32})}
        self.connection = MagicMock()
        self.headers = {'X-Hub-Client': 'crm', 'X-Hub-Key': 'k' * 32, 'X-Airpay-Owner': 'owner'}

    def test_owner_namespace_and_no_implicit_pay(self):
        # Доступ к журналу разделяется по CRM-клиенту и его пользователю.
        with patch('hub.airpay_app.build_airpay_service') as provider:
            provider.return_value.payments_enabled = False
            self.connection.return_value.__enter__.return_value.execute.return_value.fetchall.return_value = []
            client = TestClient(create_airpay_app(self.env, self.connection))
            response = client.get('/integrations/airpay/transactions', headers=self.headers)
            self.assertEqual(response.status_code, 200)
            args = self.connection.return_value.__enter__.return_value.execute.call_args.args[1]
            self.assertEqual(args[0], '["crm","owner"]')
            provider.return_value.pay.assert_not_called()
            provider.return_value.get_voucher.assert_not_called()

    def test_authentication_and_kill_switch_are_independent_of_interhub(self):
        # Наличие Interhub-флага не включает Airpay; короткий ключ и чужой клиент не принимаются.
        with patch('hub.airpay_app.build_airpay_service') as provider:
            client = TestClient(create_airpay_app({**self.env, 'INTERHUB_PAY_ENABLED': 'true', 'AIRPAY_PAYMENTS_ENABLED': 'true'}, self.connection))
            self.assertEqual(provider.call_args.args[0]['AIRPAY_PAYMENTS_ENABLED'], 'false')
            self.assertEqual(client.get('/integrations/airpay/transactions').status_code, 401)
            self.assertEqual(client.get('/integrations/airpay/transactions', headers={**self.headers, 'X-Hub-Client': 'other'}).status_code, 401)
            self.connection.assert_not_called()

    def test_vendored_runtime_matches_its_manifest(self):
        # Артефакт с собственными незаметными правками должен перестать проходить проверку поставки.
        root = Path(__file__).resolve().parents[1] / 'api/airpay_runtime'
        for filename, digest in json.loads((root / 'manifest.json').read_text()).items():
            self.assertEqual(hashlib.sha256((root / filename).read_bytes()).hexdigest(), digest['package_sha256'])

    def test_health_does_not_contact_supplier(self):
        # Проверка процесса не отправляет даже read-only запросы поставщику.
        with patch('hub.airpay_app.build_airpay_service') as provider:
            client = TestClient(create_airpay_app(self.env, self.connection))
            self.assertEqual(client.get('/live').status_code, 200)
            self.connection.assert_not_called()
            self.assertFalse(provider.return_value.mock_calls)
