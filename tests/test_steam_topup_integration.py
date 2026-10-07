"""Real PostgreSQL leases and payment state; no provider network."""
import os
import threading
import unittest
from dataclasses import replace
from decimal import Decimal
from uuid import uuid4
import psycopg
from psycopg.conninfo import conninfo_to_dict
from hub.domain import PurchaseRequest, PurchaseState
from hub.repository import PostgresPurchaseRepository, IdempotencyConflict
from hub.service import PurchaseService
from hub.providers.base import ProviderError
from tests.helpers import FakeProvider, paid, settings

DSN=os.getenv('HUB_TEST_DATABASE_URL','')

@unittest.skipUnless(DSN,'Requires isolated HUB_TEST_DATABASE_URL')
class TopupDatabaseTests(unittest.TestCase):
    def setUp(self):
        if not conninfo_to_dict(DSN).get('dbname','').startswith('hub_test_'):
            raise RuntimeError('Refusing non-test database')
        with psycopg.connect(DSN) as c:
            c.execute('TRUNCATE supplier_hub.purchases CASCADE')
            c.execute('TRUNCATE supplier_hub.topup_permissions')
            c.execute("INSERT INTO supplier_hub.topup_permissions VALUES ('seller',1,true,200),('seller',2,true,200)")
        self.repo=PostgresPurchaseRepository(DSN)
        self.provider=FakeProvider()
        self.provider.check_result=replace(self.provider.check_result,public_payload={'success':True,'status':0})
        self.service=PurchaseService(self.repo,{'interhub':self.provider},settings(database_url=DSN,topups_enabled=True))
        self.request=PurchaseRequest('seller','link:1','request:1','interhub',9361,account='test_account',
            kind='steam_topup',requested_amount=Decimal('100.00'),workspace_id=1)
        self.success=replace(paid(''),public_payload={'success':True,'status':0})

    def due(self):
        with psycopg.connect(DSN) as c:
            c.execute("UPDATE supplier_hub.purchases SET lease_until=NULL,next_attempt_at=now()-interval '1 second'")

    def step(self):
        self.due()
        purchase,lease=self.repo.claim_due(60)
        return self.service.process_claimed(purchase,lease)

    def test_concurrent_enqueue_and_claim_produce_one_purchase(self):
        barrier=threading.Barrier(2);results=[];errors=[]
        def work():
            try:barrier.wait();results.append(self.service.enqueue(self.request)[0].id)
            except Exception as exc:errors.append(exc)
        threads=[threading.Thread(target=work) for _ in range(2)]
        for t in threads:t.start()
        for t in threads:t.join(5)
        self.assertFalse(errors);self.assertEqual(len(set(results)),1)
        barrier=threading.Barrier(2);claims=[]
        def claim():
            try:barrier.wait();claims.append(self.repo.claim_due(60))
            except Exception as exc:errors.append(exc)
        threads=[threading.Thread(target=claim) for _ in range(2)]
        for t in threads:t.start()
        for t in threads:t.join(5)
        self.assertFalse(errors);self.assertEqual(sum(x is not None for x in claims),1)

    def test_timeout_restarts_into_status_not_payment(self):
        purchase,_=self.service.enqueue(self.request)
        self.assertEqual(self.step().state,PurchaseState.CHECKED)
        self.provider.pay_results=[ProviderError('timeout')]
        self.assertEqual(self.step().state,PurchaseState.PROCESSING)
        self.assertIsNotNone(self.repo.get(purchase.id).pay_started_at)
        self.provider.status_results=[self.success]
        self.service.repository=PostgresPurchaseRepository(DSN)
        self.assertEqual(self.step().state,PurchaseState.SUCCEEDED)
        self.assertEqual(self.provider.pay_calls,1)
        self.assertEqual(self.provider.status_calls,1)
        self.assertIsNone(self.repo.get(purchase.id).result_ciphertext)

    def test_crash_after_durable_payment_start_skips_pay(self):
        purchase,_=self.service.enqueue(self.request);self.step();self.due()
        claimed,lease=self.repo.claim_due(60)
        self.repo.mark_payment_started(claimed.id,lease)
        self.provider.status_results=[self.success]
        self.assertEqual(self.step().state,PurchaseState.SUCCEEDED)
        self.assertEqual(self.provider.pay_calls,0)

    def test_workspace_permission_and_immutable_fingerprint(self):
        purchase,_=self.service.enqueue(self.request)
        with self.assertRaises(IdempotencyConflict):self.service.enqueue(replace(self.request,workspace_id=2))
        with psycopg.connect(DSN) as c:c.execute('UPDATE supplier_hub.topup_permissions SET enabled=false WHERE workspace_id=1')
        self.step()
        self.assertEqual(self.repo.get(purchase.id).state,PurchaseState.CREATED)
        other,_=self.service.enqueue(replace(self.request,workspace_id=2,idempotency_key='link:2'))
        self.assertEqual(other.workspace_id,2)
