"""Payment validation logic."""

import re
from typing import Optional


class PaymentValidator:
    """Validates payment information before processing."""

    CARD_PATTERNS = {
        "visa": r"^4[0-9]{12}(?:[0-9]{3})?$",
        "mastercard": r"^5[1-5][0-9]{14}$",
        "amex": r"^3[47][0-9]{13}$"
    }

    def validate_card(self, card_token: str) -> dict:
        """
        Validate a card token.
        
        BUG: Doesn't handle expired cards properly.
        """
        if not card_token:
            return {"valid": False, "error": "No card token provided"}

        # Simulate token validation
        if card_token.startswith("tok_invalid"):
            return {"valid": False, "error": "Invalid card token"}

        if card_token.startswith("tok_expired"):
            # BUG: Should return more specific error
            return {"valid": False, "error": "Card validation failed"}

        return {"valid": True, "card_type": self._detect_card_type(card_token)}

    def validate_amount(self, amount: float) -> bool:
        """Validate payment amount."""
        if amount <= 0:
            return False
        if amount > 10000:  # Max transaction limit
            return False
        return True

    def validate_checkout(self, checkout_data: dict) -> dict:
        """Validate complete checkout data."""
        errors = []

        if not checkout_data.get("user_id"):
            errors.append("Missing user ID")

        if not checkout_data.get("card_token"):
            errors.append("Missing payment method")

        amount = checkout_data.get("total", 0)
        if not self.validate_amount(amount):
            errors.append("Invalid amount")

        return {
            "valid": len(errors) == 0,
            "errors": errors
        }

    def _detect_card_type(self, token: str) -> Optional[str]:
        """Detect card type from token prefix."""
        # In real implementation, this would decode the token
        return "credit_card"
