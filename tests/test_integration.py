"""Integration tests for fault localizer."""

import pytest
from pathlib import Path
from fault_localizer import FaultLocalizer


SAMPLE_REPO = Path(__file__).parent.parent / "sample-repo"

PYTHON_ERROR = """
Traceback (most recent call last):
  File "app/main.py", line 12, in handle_request
    result = self.processor.process(data)
  File "app/processor.py", line 13, in process
    validated = self.validator.validate(data)
  File "app/validator.py", line 14, in validate
    return self._check_schema(data)
  File "app/validator.py", line 22, in _check_schema
    raise ValueError(f"Invalid field: {field}")
ValueError: Invalid field: user_email
"""


@pytest.mark.skipif(not SAMPLE_REPO.exists(), reason="Sample repo not found")
class TestFaultLocalizer:
    def test_index_codebase(self):
        localizer = FaultLocalizer(str(SAMPLE_REPO), use_llm=False)
        count = localizer.index()
        assert count > 0

    def test_localize_without_llm(self):
        localizer = FaultLocalizer(str(SAMPLE_REPO), use_llm=False)
        localizer.index()

        results = localizer.localize(PYTHON_ERROR, top_k=5)

        assert len(results) > 0
        # Should find validator methods
        names = [r["entity"].name for r in results]
        assert any("validate" in n or "check" in n for n in names)

    def test_extracts_correct_exception(self):
        localizer = FaultLocalizer(str(SAMPLE_REPO), use_llm=False)
        error = localizer._extract_error(PYTHON_ERROR)

        assert error.exception_type == "ValueError"
        assert "user_email" in error.message
        assert len(error.frames) == 4
