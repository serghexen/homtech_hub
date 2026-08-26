# Supplier Hub architecture

## Service boundary

Supplier Hub owns supplier communication and acquisition state. Seller owns marketplace orders, fulfillment policy, pools, support messages, manual input and outbound delivery. CRM remains the active production owner until an explicit store-by-store cutover.

Supplier Hub never reads CRM or Seller tables. Consumers communicate only through authenticated HTTP requests and stable idempotency keys.

## Preserved CRM guarantees

The first InterHub adapter preserves the safety properties found in the working CRM implementation:

1. `calculate` and `check` happen before `pay`.
2. The provider operation id is persisted before external calls.
3. `payment_started` is persisted before the `pay` request.
4. Any exception after that transition is treated as uncertain and reconciled with `check_status`.
5. A succeeded result without a code remains processing.
6. A succeeded purchase is terminal and its code is stored encrypted.
7. Due work is claimed with a database lease and `FOR UPDATE SKIP LOCKED`.
8. A stable consumer idempotency key returns the original operation.
9. Reusing the same key with different arguments is rejected.
10. A result hash prevents the same supplier code from being accepted twice.

## Operator reconciliation

`requires_attention` закрывается только через отдельный операторский API. Его
учётные данные не дают доступ к API потребителей, а сервис решений не получает
экземпляр supplier provider. Поэтому операторское действие технически не может
повторить `pay` или выполнить сетевой запрос к InterHub.

Оператор после внешней проверки выбирает один из двух окончательных исходов:

- `confirm_failed` — поставщик подтвердил отсутствие списания;
- `record_success` — результат найден у поставщика и сохраняется в Hub в
  зашифрованном виде.

Каждое решение требует отдельный `X-Request-ID`, идемпотентно, выполняется под
блокировкой строки покупки и записывается в `operator_actions` и
`purchase_events`. Комментарий сохраняется для аудита, а восстановленный код не
попадает в историю действий, обычные ответы API или логи.

## Idempotency key

Seller should generate one key per required unit, for example:

```text
seller:yandex:<connection-id>:<order-id>:<item-id>:<unit-index>
```

The key must be based on immutable marketplace identifiers. A retry sends exactly the same key and request. Supplier Hub returns the existing purchase instead of creating another provider payment.

## Fallback contract

Seller may continue to its local pool only when Supplier Hub returns a definitive `failed` state before or after an explicit provider rejection.

Seller must stop fallback for:

- `payment_started`;
- `processing`;
- `succeeded`;
- `requires_attention`.

This prevents receiving a late paid InterHub code after Seller has already consumed a pool key.

## Deployment boundary

Supplier Hub is intended to run on the CRM VM because its outbound IP is allowed by InterHub. Co-location does not imply shared runtime ownership:

- separate Compose project;
- separate PostgreSQL database and database role;
- separate secrets and backups;
- resource limits before production enablement;
- API exposed only to the private network or explicitly allowed callers;
- CRM and Seller databases are not mounted or reachable by the Hub containers.

## Rollout

1. Run migrations and API with both payment switches disabled.
2. Verify readiness, authentication, catalog and balance.
3. Run provider contract tests with recorded sanitized fixtures.
4. Perform one controlled purchase through an operator-only test command.
5. Route one CRM product to Supplier Hub under an explicit feature flag; never run both paths for the same operation.
6. Add Supplier Hub purchase records and polling to Seller.
7. Implement the Seller resolver: Supplier Hub, pool, support, manual input.
8. Drain active CRM supplier attempts and switch one store.
9. Reconcile orders, codes, balances and audit events before expanding the rollout.
