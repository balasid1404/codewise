"""Data processing module."""

from validator import DataValidator
from transformer import DataTransformer


class DataProcessor:
    def __init__(self):
        self.validator = DataValidator()
        self.transformer = DataTransformer()

    def process(self, data: dict) -> dict:
        """Process incoming data."""
        validated = self.validator.validate(data)
        transformed = self.transformer.transform(validated)
        return transformed

    def batch_process(self, items: list[dict]) -> list[dict]:
        """Process multiple items."""
        return [self.process(item) for item in items]
