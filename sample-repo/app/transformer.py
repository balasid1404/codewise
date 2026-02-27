"""Data transformation module."""


class DataTransformer:
    def transform(self, data: dict) -> dict:
        """Transform validated data."""
        result = {}
        for key, value in data.items():
            result[self._normalize_key(key)] = self._normalize_value(value)
        return result

    def _normalize_key(self, key: str) -> str:
        """Convert key to snake_case."""
        return key.lower().replace("-", "_")

    def _normalize_value(self, value) -> str:
        """Normalize value to string."""
        if value is None:
            return ""
        return str(value).strip()
