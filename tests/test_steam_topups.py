from dataclasses import replace
from decimal import Decimal
from uuid import uuid4
import unittest
from unittest.mock import Mock
from hub.domain import PurchaseRequest, PurchaseState
from hub.providers.base import ProviderError
from hub.service import PurchaseService, request_fingerprint
from hub.repository import IdempotencyConflict
from hub.operator_service import OperatorService, OperatorDecision
from tests.helpers import MemoryRepository, FakeProvider, paid, settings


class SteamTopupTests(unittest.TestCase):
    def setUp(self):
        self.repo = MemoryRepository()
        self.provider = FakeProvider()
        self.provider.check_result = replace(self.provider.check_result, public_payload={'success': True, 'status': 0})
        self.provider.check = Mock(return_value=self.provider.check_result)
        self.provider.calculate = Mock(side_effect=AssertionError('TOP_UP must not calculate voucher price'))
        self.config = settings(topups_enabled=True)
        self.service = PurchaseService(self.repo, {'interhub': self.provider}, self.config)
        self.request = PurchaseRequest('seller','link:1:attempt:1','request:1','interhub',9361,
            account='test_account',kind='steam_topup',requested_amount=Decimal('100.00'),workspace_id=1)
        self.success = replace(paid(''), public_payload={'success': True, 'status': 0})

    def checked(self):
        purchase, _ = self.service.enqueue(self.request)
        return self.service.process_claimed(purchase, uuid4())

    def test_transient_check_failure_has_bounded_retry_without_pay(self):
        self.provider.check.side_effect=ProviderError('429')
        purchase,_=self.service.enqueue(self.request)
        for _ in range(5): purchase=self.service.process_claimed(purchase,uuid4())
        self.assertEqual(purchase.state,PurchaseState.FAILED)
        self.assertEqual(self.provider.pay_calls,0)
        self.assertEqual(self.provider.check.call_count,5)

    def test_topup_success_without_code_uses_exact_amount(self):
        purchase = self.checked()
        self.assertEqual(self.provider.check.call_args.args[0]['amount'],'100.00')
        self.provider.pay_results = [self.success]
        purchase = self.service.process_claimed(purchase, uuid4())
        self.assertEqual(purchase.state, PurchaseState.SUCCEEDED)
        self.assertFalse(purchase.result_available)
        for _ in range(3): self.service.process_claimed(purchase, uuid4())
        self.assertEqual(self.provider.pay_calls,1)

    def test_timeout_only_reconciles_original_payment(self):
        purchase = self.checked()
        self.provider.pay_results=[ProviderError('timeout')]
        self.provider.status_results=[self.success]
        self.service.process_claimed(purchase,uuid4())
        self.service.process_claimed(purchase,uuid4())
        self.assertEqual(purchase.state,PurchaseState.SUCCEEDED)
        self.assertEqual(self.provider.pay_calls,1)
        self.assertEqual(self.provider.status_calls,1)

    def test_crash_after_payment_started_never_pays_again(self):
        purchase=self.checked()
        self.repo.mark_payment_started(purchase.id,uuid4())
        self.provider.status_results=[self.success]
        self.service.process_claimed(purchase,uuid4())
        self.assertEqual(self.provider.pay_calls,0)
        self.assertEqual(self.provider.status_calls,1)

    def test_invalid_login_never_calls_pay(self):
        self.provider.check.return_value=replace(self.provider.check_result,success=False,status=-1,public_payload={'success':False,'status':-1})
        self.assertEqual(self.checked().state,PurchaseState.FAILED)
        self.assertEqual(self.provider.pay_calls,0)

    def test_disabled_scope_defers_checked_purchase(self):
        purchase=self.checked()
        self.repo.topup_allowed=lambda _: False
        self.service.process_claimed(purchase,uuid4())
        self.assertEqual(purchase.state,PurchaseState.CHECKED)
        self.assertEqual(self.provider.pay_calls,0)

    def test_malformed_success_is_not_success(self):
        purchase=self.checked()
        self.provider.pay_results=[paid('')]
        self.service.process_claimed(purchase,uuid4())
        self.assertEqual(purchase.state,PurchaseState.PROCESSING)

    def test_scope_amount_and_account_are_immutable(self):
        self.service.enqueue(self.request)
        for patch in ({'workspace_id':2},{'account':'other_account'},{'requested_amount':Decimal('200.00')}):
            with self.assertRaises(IdempotencyConflict):
                self.service.enqueue(replace(self.request,**patch))
            self.assertNotEqual(request_fingerprint(self.request),request_fingerprint(replace(self.request,**patch)))

    def test_operator_can_record_verified_topup_without_fictitious_code(self):
        purchase=self.checked()
        purchase.state=PurchaseState.REQUIRES_ATTENTION
        resolution=OperatorService(self.repo,'s'*32).resolve(purchase.id,'operator','decision1',OperatorDecision.RECORD_SUCCESS,'Provider confirmed completion')
        self.assertEqual(resolution.purchase.state,PurchaseState.SUCCEEDED)
        self.assertFalse(resolution.purchase.result_available)
