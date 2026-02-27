"""Checkout API controller."""

from .payment_service import PaymentService, PaymentError


class CheckoutController:
    """Handles checkout API endpoints."""

    def __init__(self):
        self.payment_service = PaymentService()

    def handle_checkout(self, request: dict) -> dict:
        """
        POST /api/checkout
        
        Called when user clicks "Pay Now" on checkout page.
        """
        try:
            result = self.payment_service.pay_now(request)
            return {"status": "success", "data": result}
        except PaymentError as e:
            # BUG: Returns generic error to frontend
            return {"status": "error", "message": str(e)}

    def get_checkout_summary(self, user_id: str) -> dict:
        """GET /api/checkout/summary"""
        return {
            "user_id": user_id,
            "items": [],
            "total": 99.99,
            "currency": "USD"
        }

    def cancel_checkout(self, session_id: str) -> dict:
        """POST /api/checkout/cancel"""
        return {"status": "cancelled", "session_id": session_id}
