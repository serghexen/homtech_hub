# Airpay в Supplier Hub

Airpay выполняется отдельным процессом `hub.airpay_app:app` (8011) и использует
схему `supplier_hub_airpay` в БД Hub. Существующие API, worker, таблицы и настройки
Interhub не изменяются. Буквенные ID Airpay не подменяются числовыми ID Interhub.
В модуле есть подготовка, долговечная очередь пачек, отдельная выдача кода,
история, шифрование новых кодов, аудит раскрытия и переоценка неоплаченного остатка.

## Источник кода

`api/airpay_runtime` — воспроизводимый исходный пакет из gamesales, собираемый
`python3 api/scripts/export_airpay_runtime.py <каталог-поставки>`.
`manifest.json` фиксирует исходные и поставленные SHA256. Не править пакет
независимо: правка и тесты выполняются в источнике, затем пакет обновляется.
Hub не импортирует CRM во время работы и не обращается к её БД.

## Подключение

В окружении Hub нужны `DATABASE_URL`, `SUPPLIER_HUB_DATA_SECRET` (не менее 32
символов, стабильный ключ шифрования), `SUPPLIER_HUB_CLIENTS_JSON` с выделенным
CRM-клиентом, `AIRPAY_USERNAME`, `AIRPAY_PASSWORD`, настройки URL/proxy/TLS Airpay.
Для предварительного запуска отключить `AIRPAY_PAYMENTS_ENABLED=false` только
у Airpay API. Общий `SUPPLIER_HUB_PURCHASES_ENABLED` не менять ради Airpay: он может
управлять действующим Interhub. Платёжным методам Airpay нужны оба разрешения.
`GAMESALES_LOCAL_UI=true` безусловно блокирует pay/выдачу и использует SSH-forward.

Самостоятельный Compose-проект Airpay не объединяется с основным файлом Interhub.
Нужны отдельный AIRPAY_ENV_FILE, существующая AIRPAY_DOCKER_NETWORK и явный
AIRPAY_RELEASE_TAG. AIRPAY_DEPLOY_PAYMENTS_ENABLED=false блокирует оплату даже
при разрешении внутри env-файла. Общий env Interhub не менять.

```sh
export AIRPAY_ENV_FILE=/absolute/path/to/.env.airpay
export AIRPAY_DOCKER_NETWORK=existing_hub_network
export AIRPAY_RELEASE_TAG=airpay-20260925
export AIRPAY_DEPLOY_PAYMENTS_ENABLED=false
python api/scripts/airpay_deploy_preflight.py
docker compose -f docker-compose.airpay.yml build airpay-api airpay-migrate
docker compose -f docker-compose.airpay.yml run --rm --no-deps airpay-migrate
docker compose -f docker-compose.airpay.yml up -d --no-deps --no-build airpay-api
```

Preflight не запускает сервисы и не выводит env; при ошибке следующие команды
не выполнять. В новом проекте нет postgres/worker Interhub и их томов.
API ограничен 16 параллельными запросами; его SQL имеет statement_timeout=15s,
lock_timeout=3s и connect_timeout=5s. Docker context Airpay исключает env/ключи.

Это инструкция для согласованного развёртывания, не выполненное действие.
Новый модуль не требуется запускать вместе с worker Interhub. Перед production
миграцией проверить резервную копию. Восстановление секрета шифрования обязательно
для восстановления кодов из БД; не менять его произвольно.

CRM: `AIRPAY_BACKEND=hub`, `AIRPAY_HUB_URL=http://<airpay-api>:8011`,
`AIRPAY_HUB_CLIENT_ID`, `AIRPAY_HUB_CLIENT_KEY`. Адрес доступен только по внутренней
сети/туннелю. Прямой путь CRM не регистрируется в этом режиме, fallback отсутствует.
Перед переключением завершить все старые оплаты/задания CRM. Старые записи не
копируются и не переоплачиваются. Их история остаётся отдельным архивом CRM.

API модуля сохраняет маршруты `/integrations/airpay/*`. Аутентификация:
`X-Hub-Client`, `X-Hub-Key`, `X-Airpay-Owner` (URL-encoded имя пользователя CRM).
Владелец хранения — пара client/owner; нельзя прочитать чужую операцию другим
клиентом. Подготовка не покупает; чтение jobs/истории не повторяет check/pay.
`POST transactions/{id}/result` читает сохранённый код с аудитом, не вызывает Airpay.
`reconcile` — платёжный метод и требует явного пользовательского действия.

Общая существующая таблица Hub для Interhub имеет другой контракт и не изменялась.
История Airpay принадлежит этому модулю Hub и доступна через тот же экран CRM.
Новые операции не дублируются в старую таблицу `supplier_hub.purchases`.

## Контракт airpay.v1 (первый шаг внедрения)

Airpay API возвращает X-Airpay-Contract: airpay.v1 и принимает POST только с этой
версией. GET /integrations/airpay/contract проверяется прокси CRM перед каждым
POST; execution_backend должен быть hub. Обновлять изолированные Airpay CRM/Hub
нужно согласованно, без смешанного запуска старых и новых версий. Interhub не
меняется. Окружения этой доработкой не переключены.

Карточка операции разделяет payment_state и result_state: paid-ваучер без кода
остаётся pending/completed=false, пополнение завершается без кода. pin_code
отсутствует в обычном сериализаторе; раскрытие отдельно протоколируется.

Перед переходом CRM → Hub проверить GET /integrations/airpay/cutover в CRM:
processing, все активные задания и paid-ваучеры без сохранённого кода блокируют
новые покупки. Сначала завершить старые операции и остановить прямой исполнитель,
затем повторить проверку. Чтение истории не выполняет pay. Полный план и контракт:
gamesales/docs/airpay-rollout-plan.md и gamesales/docs/airpay-contract.md.

## Журнал и ручные решения

Миграции 20260923_05/06 добавляют события, ручные решения и признаки необходимости
разбора. Readiness Airpay проверяет новые таблицы. Сначала миграции, затем запуск
обновлённого Airpay API; API/worker Interhub не меняются.

GET /integrations/airpay/transactions/{id}/events читает журнал. POST по тому же
пути с /resolve фиксирует результат внешней сверки владельца, без объекта
транспорта и без pay. Решение требует UUID, свежего updated_at, основания и
подтверждения; успешный ваучер требует кода, оплаченный результат нельзя отменить.
Решение и DB-событие атомарны, повтор идемпотентен, код хранится зашифрованным.

После пяти незавершённых запросов pay/voucher операция требует разбора и не
посылает новые запросы поставщику. Доступны чтение и ручное решение владельца.
Подробный контракт: gamesales/docs/airpay-contract.md. В этой доработке мигрирована
только тестовая БД CRM; миграции Hub и его развёртывание не выполнялись.

## Поиск и Excel

Airpay history поддерживает q, status, kind, date_from/date_to (даты создания,
обе границы включительно по МСК). GET transactions/export выгружает ту же выборку
целиком, до 10 000 операций, без кодов и сырых запросов/ответов поставщика.
При превышении лимита возвращается 422 с просьбой уточнить фильтры.
Архив CRM фильтруется и выгружается самой CRM; он не отправляется в Hub.
Новая зависимость openpyxl указана только в requirements-airpay.txt.
Схема БД не менялась; требуется пересборка только отдельного Airpay API.

## Восстановление очереди

После потери session lock начатые running pay/voucher/reconcile завершаются
ошибкой задания без повторного вызова поставщика и без изменения транзакции.
Подтверждённые queued выполняются при разрешении оплаты; running check/renew
восстанавливаются по прежним данным. При штатной остановке следующая позиция
пачки не начинается, API ждёт завершения потока не более 5 секунд.

GET /integrations/airpay/queue читает только собственные задания client/owner:
ожидание, running, отсутствие прогресса 5 минут, запрет настройкой оплаты,
ошибки за 24 часа и DB-lock исполнителя. Коды и payload не возвращаются.
POST jobs/{uuid}/cancel отменяет только queued под тем же lock, что worker.
Отмена задания не является отменой оплаты; начатое задание возвращает 409.

При включении оплаты сначала проверить queued, чтобы не запустить забытое
подтверждённое действие. Внешние оповещения при развёртывании подключаются отдельно;
диагностика БД не является heartbeat всех worker. Схема не менялась.
Полный порядок восстановления: gamesales/docs/airpay-operations.md.
