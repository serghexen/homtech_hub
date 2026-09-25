"""Диагностика только своей очереди Airpay: чтение БД без запросов поставщику."""
from psycopg.rows import dict_row

STALE_SECONDS = 300


def queue_diagnostics(connect, owner, payments_enabled):
    # Старый updated_at — повод проверить worker, но не доказательство отказа оплаты.
    with connect(row_factory=dict_row) as conn:
        summary = conn.execute("""SELECT
            count(*) FILTER (WHERE state='queued') AS queued,
            count(*) FILTER (WHERE state='running') AS running,
            count(*) FILTER (WHERE state IN ('queued','running') AND updated_at < now() - interval '5 minutes') AS stale,
            count(*) FILTER (WHERE state='queued' AND action IN ('pay','voucher','reconcile') AND NOT %s) AS blocked_by_config,
            count(*) FILTER (WHERE state='failed' AND updated_at >= now() - interval '24 hours') AS failed_24h
            FROM supplier_hub_airpay.airpay_jobs WHERE created_by=%s""", (payments_enabled, owner)).fetchone()
        rows = conn.execute("""SELECT j.id::text AS job_id,j.transaction_id::text,j.action,j.state,j.progress,j.created_at,j.updated_at,
            j.updated_at < now() - interval '5 minutes' AS stale,
            EXISTS (SELECT 1 FROM pg_locks l WHERE l.locktype='advisory' AND l.granted AND l.objsubid=1
                AND l.database=(SELECT oid FROM pg_database WHERE datname=current_database())
                AND l.classid=((hashtextextended('airpay-job:' || j.id::text,0) >> 32) & 4294967295)::oid
                AND l.objid=(hashtextextended('airpay-job:' || j.id::text,0) & 4294967295)::oid) AS worker_lock_held
            FROM supplier_hub_airpay.airpay_jobs j WHERE j.created_by=%s AND
                (j.state IN ('queued','running') OR (j.state='failed' AND j.updated_at >= now() - interval '24 hours'))
            ORDER BY (j.state IN ('queued','running')) DESC,j.created_at,j.id LIMIT 51""", (owner,)).fetchall()
    items = []
    for row in rows[:50]:
        # Выводим только ID и прогресс; тело задания, реквизиты и сохранённый результат не читаем.
        reason = ('interrupted' if row['state'] == 'running' and not row['worker_lock_held'] else
                  'payments_disabled' if row['state'] == 'queued' and row['action'] in {'pay', 'voucher', 'reconcile'} and not payments_enabled else
                  'failed' if row['state'] == 'failed' else 'stale' if row['stale'] else '')
        items.append({**row, 'reason': reason})
    return {'summary': dict(summary), 'items': items, 'has_more': len(rows) > 50,
            'stale_after_seconds': STALE_SECONDS, 'payments_enabled': payments_enabled}
