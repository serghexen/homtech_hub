# Read-only остатки для Seller — production 06.10.2026

Ревизия `a1458830653b78f66dd58b29471ac65b8b2941d8` отправлена в origin/main до выкладки.
В действующий `/home/adminops/homtech-hub-staging` доставлены только `api/hub/app.py` и `api/hub/stock_snapshot.py` из git archive этого коммита. Manifest с ревизией и SHA256 проверен до установки и внутри запущенного контейнера. Остальные 11 Python-модулей Hub побайтно совпадают с прежним API-образом.

Рабочий API использует образ `sha256:9949c329c74d570963d8e10ec55f9614861399944141f1967f397784f5704269`. Покупочный worker не перезапускался: прежний image `sha256:7f63972622d9e1987e67eb1c089de1ae2c5c85909c389f915731d5bbca2ea38b`, запуск 26.08.2026. Airpay также не менялся.

В runtime .env.staging настроены отдельный машинный секрет и URL `http://gamesales-api:8000/internal/supplier-stock/interhub` в уже существующей закрытой Docker-сети. Секрет не попал в Git, логи и вывод. Endpoint по-прежнему защищён client-аутентификацией Hub. Покупочный API не менялся.

Live/ready успешны, цепочка CRM → Hub → Seller отдала 701 сохранённую позицию без ошибки каталога. 55 локальных тестов прошли. Дополнительных опросов Интерхаба и тестовых покупок не выполнялось.

Исходный образ сохранён как `homtech-hub-staging-api:before-stock-a145883`; настройки, архив и manifest — `/home/adminops/backups/supplier-stock-20261006`. Откат API снимает только доступность нового снимка: Seller должен предупредить об общей ошибке, а не массово обнулить карточки.

Полный отчёт: [GameSales release report](https://github.com/serghexen/gamesales/blob/main/docs/supplier-stock-release-20261006.md).
