"""Durable polling worker for Supplier Hub purchases."""

from __future__ import annotations

import logging
import signal
import time

from hub.config import load_settings
from hub.providers.interhub import InterHubProvider
from hub.repository import PostgresPurchaseRepository
from hub.service import PurchaseService


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOGGER = logging.getLogger("supplier-hub-worker")
STOP = False


def request_stop(_signum, _frame) -> None:
    global STOP
    STOP = True


def main() -> None:
    settings = load_settings()
    errors = settings.readiness_errors()
    if errors:
        raise RuntimeError("; ".join(errors))
    repository = PostgresPurchaseRepository(settings.database_url)
    provider = InterHubProvider(settings)
    service = PurchaseService(repository, {provider.code: provider}, settings)

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    paused_logged = False
    while not STOP:
        if not settings.live_pay_allowed:
            if not paused_logged:
                LOGGER.info("purchases are paused by kill switches")
                paused_logged = True
            time.sleep(settings.worker_poll_sec)
            continue
        paused_logged = False
        claimed = repository.claim_due(settings.lease_sec)
        if not claimed:
            time.sleep(settings.worker_poll_sec)
            continue
        purchase, lease_token = claimed
        try:
            result = service.process_claimed(purchase, lease_token)
            LOGGER.info(
                "purchase processed id=%s provider=%s state=%s",
                result.id,
                result.provider_code,
                result.state,
            )
        except Exception:
            # The DB lease expires and the durable state is safely claimed again. Never log request params or secrets.
            LOGGER.exception("purchase processing failed id=%s state=%s", purchase.id, purchase.state)


if __name__ == "__main__":
    main()
