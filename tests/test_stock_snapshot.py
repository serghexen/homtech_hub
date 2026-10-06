import unittest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
from hub.app import create_app
from hub.stock_snapshot import read_stock_snapshot
from tests.helpers import settings


class StockSnapshotTests(unittest.TestCase):
    def test_snapshot_requires_existing_hub_auth_and_does_not_call_provider(self):
        # Проверяем отдельный read-only маршрут с тем же машинным доступом, что и каталог.
        client=TestClient(create_app(settings()))
        with patch('hub.app.read_stock_snapshot',return_value={'version':1,'items':[]}) as read:
            self.assertEqual(client.get('/v1/providers/interhub/stock-snapshot').status_code,401)
            read.assert_not_called()
            result=client.get('/v1/providers/interhub/stock-snapshot',headers={'X-Hub-Client':'seller','X-Hub-Key':'s'*32})
            self.assertEqual(result.status_code,200)
            read.assert_called_once_with()

    def test_source_errors_do_not_leak_credentials_or_become_zero(self):
        # Текст транспорта может содержать адрес или секрет, поэтому наружу выходит только общий 503.
        client=TestClient(create_app(settings()))
        with patch('hub.app.read_stock_snapshot',side_effect=ValueError('secret-token')):
            result=client.get('/v1/providers/interhub/stock-snapshot',headers={'X-Hub-Client':'seller','X-Hub-Key':'s'*32})
        self.assertEqual(result.status_code,503)
        self.assertNotIn('secret-token',result.text)
        self.assertNotIn('items',result.json())

    def test_external_plain_http_is_rejected_before_network(self):
        # Снимок нельзя читать с передачей секрета через незашифрованный внешний адрес.
        with patch.dict('os.environ',{'SUPPLIER_STOCK_SNAPSHOT_URL':'http://example.com/data','SUPPLIER_STOCK_SNAPSHOT_TOKEN':'test'}):
            with patch('hub.stock_snapshot.urllib.request.build_opener') as opener:
                with self.assertRaises(RuntimeError): read_stock_snapshot()
                opener.assert_not_called()
