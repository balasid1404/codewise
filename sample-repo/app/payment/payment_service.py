"""Payment processing service."""

from typing import Optional
from .payment_validator import PaymentValidator
from .billing_client import BillingClient


class PaymentService:
    """Handles payment processing for music subscriptions."""

    def __init__(self):
        self.validator = PaymentValidator()
        self.billing = BillingClient()

    def process_payment(self, user_id: str, amount: float, card_token: str) -> dict:
        """
        Process a payment for subscription.
        
        This is the main entry point for the "Pay Now" button.
        """
        # Validate card first
        validation = self.validator.validate_card(card_token)
        if not validation["valid"]:
            raise PaymentError(f"Card validation failed: {validation['error']}")

        # Attempt charge
        try:
            result = self.billing.charge(user_id, amount, card_token)
            return {"success": True, "transaction_id": result["id"]}
        except BillingError as e:
            # BUG: This error message is too generic
            raise PaymentError("Payment could not be processed. Please try again.")

    def pay_now(self, checkout_data: dict) -> dict:
        """Handle Pay Now button click from checkout page."""
        user_id = checkout_data.get("user_id")
        amount = checkout_data.get("total")
        card_token = checkout_data.get("card_token")

        if not all([user_id, amount, card_token]):
            raise PaymentError("Missing required checkout data")

        return self.process_payment(user_id, amount, card_token)

    def handle_payment(self, request: dict) -> dict:
        """API handler for payment requests."""
        return self.pay_now(request)


class PaymentError(Exception):
    """Payment processing error."""
    pass


class BillingError(Exception):
    """Billing system error."""
    pass
