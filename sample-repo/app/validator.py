"""Data validation module - contains the bug."""

import re

EMAIL_PATTERN = re.compile(r'^[\w\.-]+@[\w\.-]+\.\w+$')


class DataValidator:
    def __init__(self):
        self.required_fields = ["user_email", "user_name"]

    def validate(self, data: dict) -> dict:
        """Validate incoming data."""
        self._check_required(data)
        return self._check_schema(data)

    def _check_required(self, data: dict) -> None:
        """Check required fields exist."""
        for field in self.required_fields:
            if field not in data:
                raise ValueError(f"Missing required field: {field}")

    def _check_schema(self, data: dict) -> dict:
        """Validate field formats - BUG: doesn't handle missing user_name."""
        for field, value in data.items():
            if field == "user_email":
                # Bug: raises confusing error instead of checking format
                if not EMAIL_PATTERN.match(value):
                    raise ValueError(f"Invalid field: {field}")
        return data

    def _validate_email(self, email: str) -> bool:
        """Validate email format."""
        return bool(EMAIL_PATTERN.match(email))
