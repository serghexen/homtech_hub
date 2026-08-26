# Supplier Hub staging

## Размещение

- VM: CRM host с разрешённым исходящим IP InterHub;
- каталог: `/home/adminops/homtech-hub-staging`;
- Compose project: `homtech-hub-staging`;
- API: `127.0.0.1:8010`, извне не опубликован;
- PostgreSQL: отдельный контейнер и volume `homtech-hub-staging_supplier_hub_postgres`.

Staging не использует сеть, БД, миграции или секреты CRM. Контейнеры имеют отдельные лимиты CPU/RAM.

## Текущее безопасное состояние

```dotenv
SUPPLIER_HUB_PURCHASES_ENABLED=false
INTERHUB_PAY_ENABLED=false
INTERHUB_TOKEN=
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

## Smoke-тест без поставщика

```bash
sudo docker exec homtech-hub-staging-api-1 python api/scripts/staging_smoke_test.py
```

Тест проверяет API-аутентификацию, идемпотентность, конфликт параметров, PostgreSQL lease/state transitions и pgcrypto. Он отказывается работать при разрешённых live-покупках и удаляет только созданную им запись в `finally`.

## Ограничения эксплуатации

- не включать payment-флаги до отдельного этапа с contract fixtures и операторской командой;
- не добавлять токен InterHub в Git или Docker image;
- не выполнять `docker compose down -v`: ключ `-v` удалит отдельную БД staging;
- не подключать контейнеры к сети Compose CRM;
- не использовать БД CRM в `DATABASE_URL` Supplier Hub.
