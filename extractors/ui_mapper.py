"""Map UI elements to code locations."""

import re
from dataclasses import dataclass


@dataclass
class UIMapping:
    ui_text: str
    code_patterns: list[str]
    likely_files: list[str]


# Common UI-to-code mappings
UI_CODE_MAPPINGS = {
    # Payment related
    "payment": ["payment", "pay", "checkout", "billing", "transaction", "charge"],
    "pay now": ["processPayment", "submitPayment", "handlePayment", "payNow", "pay_now"],
    "checkout": ["checkout", "cart", "order", "purchase"],
    "card": ["card", "credit", "payment_method", "stripe", "paymentMethod"],
    
    # Auth related
    "login": ["login", "signin", "auth", "authenticate", "signIn"],
    "sign up": ["signup", "register", "createAccount", "sign_up", "create_user"],
    "password": ["password", "credential", "auth"],
    
    # Common actions
    "submit": ["submit", "save", "create", "post", "handle"],
    "cancel": ["cancel", "abort", "close", "dismiss"],
    "delete": ["delete", "remove", "destroy"],
    "edit": ["edit", "update", "modify"],
    "search": ["search", "find", "query", "filter"],
    
    # Error states
    "error": ["error", "exception", "fail", "invalid"],
    "loading": ["loading", "spinner", "fetch", "async"],
    "timeout": ["timeout", "retry", "connection"],
}


class UIMapper:
    """Map UI elements to likely code locations."""

    def __init__(self):
        self.mappings = UI_CODE_MAPPINGS

    def get_code_patterns(self, ui_text: str) -> list[str]:
        """Get likely code patterns for a UI element."""
        ui_lower = ui_text.lower()
        patterns = []

        # Direct mappings
        for ui_key, code_patterns in self.mappings.items():
            if ui_key in ui_lower or ui_lower in ui_key:
                patterns.extend(code_patterns)

        # Generate variations of the UI text itself
        # "Pay Now" -> ["payNow", "pay_now", "PayNow", "paynow"]
        words = re.findall(r'\w+', ui_text)
        if words:
            # camelCase
            camel = words[0].lower() + "".join(w.title() for w in words[1:])
            patterns.append(camel)

            # snake_case
            snake = "_".join(w.lower() for w in words)
            patterns.append(snake)

            # PascalCase
            pascal = "".join(w.title() for w in words)
            patterns.append(pascal)

            # lowercase joined
            patterns.append("".join(w.lower() for w in words))

        return list(set(patterns))

    def suggest_file_patterns(self, app_section: str) -> list[str]:
        """Suggest file path patterns based on app section."""
        section_lower = app_section.lower()

        patterns = []

        # Common directory structures
        patterns.append(f"*{section_lower}*")
        patterns.append(f"*/{section_lower}/*")
        patterns.append(f"*/components/{section_lower}*")
        patterns.append(f"*/pages/{section_lower}*")
        patterns.append(f"*/screens/{section_lower}*")
        patterns.append(f"*/services/{section_lower}*")
        patterns.append(f"*/handlers/{section_lower}*")

        return patterns

    def build_search_context(self, extracted: dict) -> dict:
        """Build comprehensive search context from extracted image data."""
        code_patterns = []
        file_patterns = []

        # From UI elements
        for elem in extracted.get("ui_elements", []):
            code_patterns.extend(self.get_code_patterns(elem))

        # From app section
        if extracted.get("app_section"):
            code_patterns.extend(self.get_code_patterns(extracted["app_section"]))
            file_patterns.extend(self.suggest_file_patterns(extracted["app_section"]))

        # From keywords
        for kw in extracted.get("keywords", []):
            code_patterns.extend(self.get_code_patterns(kw))

        return {
            "code_patterns": list(set(code_patterns)),
            "file_patterns": list(set(file_patterns)),
            "error_text": extracted.get("error_message", ""),
            "context": extracted.get("user_action", "")
        }
