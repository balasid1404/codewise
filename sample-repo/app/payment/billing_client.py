"""Billing system client."""

import random
from typing import Optional


class BillingClient:
    """Client for external billing/payment gateway."""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or "test_key"

    def charge(self, user_id: str, amount: float, card_token: str) -> dict:
        """
        Charge a card.
        
        BUG: Random failures not handled gracefully upstream.
        """
        # Simulate API call to payment gateway
        if self._should_fail():
            raise BillingError("Gateway timeout")

        if card_token.startswith("tok_decline"):
            raise BillingError("Card declined by issuer")

        return {
            "id": f"txn_{random.randint(10000, 99999)}",
            "status": "completed",
            "amount": amount
        }

    def refund(self, transaction_id: str, amount: Optional[float] = None) -> dict:
        """Process a refund."""
        return {
            "id": f"ref_{random.randint(10000, 99999)}",
            "original_transaction": transaction_id,
            "status": "refunded"
        }

    def get_transaction(self, transaction_id: str) -> dict:
        """Get transaction details."""
        return {
            "id": transaction_id,
            "status": "completed"
        }

    def _should_fail(self) -> bool:
        """Simulate random gateway failures (10% rate)."""
        return random.random() < 0.1


class BillingError(Exception):
    """Billing gateway error."""
    pass
