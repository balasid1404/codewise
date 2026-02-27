"""Tests for image-based fault localization."""

import pytest
from extractors.ui_mapper import UIMapper
from extractors.image_extractor import ImageExtractor


class TestUIMapper:
    def setup_method(self):
        self.mapper = UIMapper()

    def test_get_code_patterns_payment(self):
        patterns = self.mapper.get_code_patterns("Pay Now")
        assert "payNow" in patterns
        assert "pay_now" in patterns
        assert "PayNow" in patterns

    def test_get_code_patterns_login(self):
        patterns = self.mapper.get_code_patterns("Sign In")
        assert "signIn" in patterns
        assert "sign_in" in patterns

    def test_suggest_file_patterns(self):
        patterns = self.mapper.suggest_file_patterns("payment")
        assert any("payment" in p for p in patterns)

    def test_build_search_context(self):
        extracted = {
            "error_message": "Payment failed",
            "ui_elements": ["Pay Now", "Credit Card"],
            "app_section": "checkout",
            "user_action": "completing purchase",
            "keywords": ["payment", "transaction"]
        }
        context = self.mapper.build_search_context(extracted)

        assert len(context["code_patterns"]) > 0
        assert "payNow" in context["code_patterns"] or "pay_now" in context["code_patterns"]
        assert len(context["file_patterns"]) > 0


class MockImageExtractor:
    """Mock extractor that returns predefined results for testing."""

    def extract_from_image(self, image_path: str) -> dict:
        # Simulate what Claude would extract from a payment error screenshot
        if "payment" in image_path.lower():
            return {
                "error_message": "Payment could not be processed. Please try again.",
                "ui_elements": ["Pay Now", "Credit Card", "Total: $99.99", "Checkout"],
                "app_section": "payment",
                "user_action": "attempting to complete payment for music subscription",
                "keywords": ["payment", "transaction", "checkout", "billing"],
                "raw_text": "Checkout - Payment Error - Payment could not be processed. Please try again. Pay Now Credit Card **** 1234 Total: $99.99"
            }
        elif "login" in image_path.lower():
            return {
                "error_message": "Invalid credentials",
                "ui_elements": ["Sign In", "Email", "Password", "Forgot Password"],
                "app_section": "authentication",
                "user_action": "trying to log in",
                "keywords": ["login", "auth", "credentials"],
                "raw_text": "Sign In - Invalid credentials - Email Password Forgot Password"
            }
        else:
            return {
                "error_message": "Something went wrong",
                "ui_elements": ["Retry", "Go Back"],
                "app_section": "unknown",
                "user_action": "unknown action",
                "keywords": ["error"],
                "raw_text": "Error - Something went wrong"
            }

    def build_search_query(self, extracted: dict) -> str:
        parts = []
        if extracted.get("error_message"):
            parts.append(extracted["error_message"])
        if extracted.get("app_section"):
            parts.append(extracted["app_section"])
        parts.extend(extracted.get("keywords", []))
        return " ".join(parts)


class TestImageLocalizationPipeline:
    """Test the full image localization pipeline with mocks."""

    def setup_method(self):
        self.extractor = MockImageExtractor()
        self.mapper = UIMapper()

    def test_payment_error_extraction(self):
        extracted = self.extractor.extract_from_image("payment_error.png")

        assert extracted["app_section"] == "payment"
        assert "Pay Now" in extracted["ui_elements"]
        assert "payment" in extracted["keywords"]

    def test_payment_to_code_mapping(self):
        extracted = self.extractor.extract_from_image("payment_error.png")
        context = self.mapper.build_search_context(extracted)

        # Should generate payment-related code patterns
        patterns = context["code_patterns"]
        assert any("pay" in p.lower() for p in patterns)
        assert any("checkout" in p.lower() for p in patterns)

    def test_login_error_extraction(self):
        extracted = self.extractor.extract_from_image("login_error.png")
        context = self.mapper.build_search_context(extracted)

        patterns = context["code_patterns"]
        assert any("sign" in p.lower() or "login" in p.lower() for p in patterns)

    def test_search_query_building(self):
        extracted = self.extractor.extract_from_image("payment_error.png")
        query = self.extractor.build_search_query(extracted)

        assert "payment" in query.lower()
        assert len(query) > 10


# Example of what a real test image scenario would look like
SAMPLE_PAYMENT_ERROR_SCENARIO = """
Screenshot description:
- Mobile app screen showing checkout page
- Red error banner at top: "Payment could not be processed"
- Form fields: Credit card number (masked), expiry, CVV
- "Pay Now" button (grayed out)
- Total amount: $99.99
- App: Music streaming subscription

Expected code locations:
1. PaymentService.processPayment() - handles payment processing
2. CheckoutController.handlePayment() - controller endpoint
3. PaymentValidator.validateCard() - card validation
4. BillingService.charge() - actual charge logic
"""


def test_scenario_documentation():
    """Document the expected behavior for manual testing."""
    print("\n" + "=" * 60)
    print("IMAGE LOCALIZATION TEST SCENARIO")
    print("=" * 60)
    print(SAMPLE_PAYMENT_ERROR_SCENARIO)
    print("\nTo test with a real image:")
    print("1. Take a screenshot of a payment error")
    print("2. Save as fault-localization/test-images/payment_error.png")
    print("3. Run: python -c \"")
    print("   from fault_localizer_prod import FaultLocalizerProd")
    print("   loc = FaultLocalizerProd(use_llm=True)")
    print("   results = loc.localize_from_image('test-images/payment_error.png')")
    print("   print(results)\"")
