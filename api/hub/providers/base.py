"""Provider adapter contract shared by InterHub and future suppliers."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol


class ProviderState(StrEnum):
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True)
class ProviderResult:
    state: ProviderState
    success: bool
    status: int
    message: str
    amount: Decimal = Decimal("0")
    fixed_amount: Decimal = Decimal("0")
    provider_transaction_id: str = ""
    secret_value: str = ""
    public_payload: dict[str, Any] = field(default_factory=dict)


class ProviderError(RuntimeError):
    """A provider call failed before a definitive business response was received."""


class SupplierProvider(Protocol):
    code: str

    def services(self) -> list[dict[str, Any]]: ...

    def balance(self) -> dict[str, Any]: ...

    def calculate(self, payload: dict[str, Any]) -> ProviderResult: ...

    def check(self, payload: dict[str, Any]) -> ProviderResult: ...

    def pay(self, provider_operation_id: str) -> ProviderResult: ...

    def check_status(self, provider_operation_id: str) -> ProviderResult: ...
