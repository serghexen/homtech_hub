"""Read-only bridge to the existing CRM stock snapshot; never calls Interhub."""
import json
import os
import urllib.request
from urllib.parse import urlsplit


def read_stock_snapshot():
    # Адрес задаёт оператор; секрет не отправляется при перенаправлении или через HTTP наружу.
    url = os.getenv('SUPPLIER_STOCK_SNAPSHOT_URL', '')
    token = os.getenv('SUPPLIER_STOCK_SNAPSHOT_TOKEN', '')
    parsed = urlsplit(url)
    if not token or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise RuntimeError('Stock snapshot is not configured')
    if parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in {'localhost', '127.0.0.1', 'api', 'gamesales-api', 'host.docker.internal'}):
        raise RuntimeError('Stock snapshot requires HTTPS')
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            # Машинный токен нельзя передавать неизвестному получателю редиректа.
            return None
    request = urllib.request.Request(url, headers={'X-Supplier-Stock-Token': token})
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=10) as response:
        raw = response.read(4 * 1024 * 1024 + 1)
    if len(raw) > 4 * 1024 * 1024:
        raise RuntimeError('Stock snapshot is too large')
    result = json.loads(raw)
    if not isinstance(result, dict) or result.get('version') != 1 or not isinstance(result.get('items'), list):
        raise RuntimeError('Invalid stock snapshot')
    return result
