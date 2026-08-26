# Supplier Hub staging

## Размещение

- VM: CRM host с разрешённым исходящим IP InterHub;
- каталог: `/home/adminops/homtech-hub-staging`;
- Compose project: `homtech-hub-staging`;
- API: `127.0.0.1:8010`, извне не опубликован;
- PostgreSQL: отдельный контейнер и volume `homtech-hub-staging_supplier_hub_postgres`.

Staging не использует сеть, БД или миграции CRM. Контейнеры имеют отдельные
лимиты CPU/RAM. Учётные данные InterHub хранятся только во внешнем
`.env.staging`, не входят в Git или Docker image и нужны для GET-проверок
каталога и баланса при выключенных покупках.

## Текущее безопасное состояние

```dotenv
SUPPLIER_HUB_PURCHASES_ENABLED=false
INTERHUB_PAY_ENABLED=false
INTERHUB_TOKEN=<configured-outside-git>
SUPPLIER_HUB_OPERATORS_JSON=<configured-outside-git>
```

Worker запускается, но остаётся в paused-режиме. Ни `calculate`, ни `check`, ни `pay`, ни `check_status` автоматически не вызываются.

## Проверка состояния

```bash
cd /home/adminops/homtech-hub-staging
sudo docker compose -p homtech-hub-staging --env-file .env.staging ps
curl -fsS http://127.0.0.1:8010/live
curl -fsS http://127.0.0.1:8010/ready
sudo docker compose -p homtech-hub-staging --env-file .env.staging logs --tail=100 api worker migrate
```

Ожидаемый readiness содержит `"purchases_enabled": false`, а worker пишет `purchases are paused by kill switches`.

## Read-only проверка InterHub

```bash
sudo docker exec homtech-hub-staging-api-1 python api/scripts/provider_readonly_check.py
```

Проверка сначала убеждается, что live-покупки выключены, затем вызывает через
Hub только `services` и `balance`. Она выводит количество услуг и валюту, но не
показывает токен или фактический баланс. При включённых payment-флагах скрипт
завершится до обращения к провайдеру.

## Smoke-тест без поставщика

```bash
sudo docker exec homtech-hub-staging-api-1 python api/scripts/staging_smoke_test.py
```

Тест проверяет API-аутентификацию, идемпотентность, сохранение исходного
`request_id`, безопасную историю событий, observability summary, PostgreSQL
lease/state transitions и pgcrypto. Он отказывается работать при разрешённых
live-покупках, не вызывает InterHub и удаляет созданную им запись и события в
`finally`.

## Операторский smoke-тест без поставщика

```bash
sudo docker exec homtech-hub-staging-api-1 python api/scripts/operator_smoke_test.py
```

Тест создаёт только синтетические строки `requires_attention`, проверяет
отдельную авторизацию, оба локальных решения, идемпотентность, аудит и
шифрование восстановленного результата. Он отказывается запускаться, если хотя
бы один payment-флаг включён, не вызывает InterHub и удаляет тестовые покупки,
события и операторские действия в `finally`.

## Ограничения эксплуатации

- не включать payment-флаги до отдельного этапа с contract fixtures и операторской командой;
- не добавлять токен InterHub в Git или Docker image;
- не выполнять `docker compose down -v`: ключ `-v` удалит отдельную БД staging;
- не подключать контейнеры к сети Compose CRM;
- не использовать БД CRM в `DATABASE_URL` Supplier Hub.
