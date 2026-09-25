"""HTTPS через локальный SSH-forward с сохранением имени сервера и проверки TLS."""

import http.client
import socket
import urllib.error
import urllib.request


class TunnelHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, *, tunnel_port, **kwargs):
        # Host остаётся именем Интерхаба; только TCP-соединение идёт в локальный SSH-forward.
        super().__init__(host, **kwargs)
        self.tunnel_port = tunnel_port

    def connect(self):
        # Проверяем сертификат Интерхаба, а не localhost; сервер SSH не расшифровывает HTTPS.
        sock = socket.create_connection(('127.0.0.1', self.tunnel_port), self.timeout, self.source_address)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except Exception:
            sock.close()
            raise


class TunnelHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, *, context, tunnel_port):
        # Отдельный обработчик применяется только к запросам Интерхаба.
        super().__init__(context=context)
        self.tunnel_port = tunnel_port

    def https_open(self, request):
        # Передаём исходное имя в HTTPSConnection для правильного Host и SNI.
        def connection(host, **kwargs):
            # Создаём новое TLS-соединение на каждый запрос через тот же SSH-туннель.
            return TunnelHTTPSConnection(host, tunnel_port=self.tunnel_port, **kwargs)
        return self.do_open(connection, request, context=self._context)


class NoTunnelRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Не позволяем редиректу обойти туннель или перенести токен на другой адрес.
        raise urllib.error.URLError('InterHub SSH tunnel does not allow redirects')
