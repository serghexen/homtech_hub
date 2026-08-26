# InterHub contract fixtures

`checked.json`, `paid.json`, `failed.json`, `services.json`, and
`balance.json` preserve response shapes observed through the production CRM on
2026-08-26. Collection used short read-only database queries and GET-only
provider calls. Every scalar that could identify an account, transaction,
voucher, balance, product, or provider message was replaced in memory before
the fixture was written.

`processing.json` is synthetic because no processing transaction existed at
collection time. Its `success=true, status=1` combination follows the behavior
already used by the production CRM.

Never replace these files with raw provider responses. Use
`api/scripts/export_crm_interhub_fixtures.py`, run its tests, and perform an
independent secret scan before accepting refreshed fixtures.
