import re
from .base import BaseExtractor, StackFrame, ExtractedError


class PythonStackExtractor(BaseExtractor):
    # Pattern: File "path.py", line 42, in method_name
    FRAME_PATTERN = re.compile(
        r'File "([^"]+)", line (\d+), in (\w+)'
    )
    # Pattern: ExceptionType: message
    EXCEPTION_PATTERN = re.compile(
        r'^(\w+(?:Error|Exception|Warning)?): (.+)$', re.MULTILINE
    )
    # Detect Python traceback
    TRACEBACK_MARKER = re.compile(r'Traceback \(most recent call last\)')

    def can_parse(self, error_text: str) -> bool:
        return bool(self.TRACEBACK_MARKER.search(error_text))

    def extract(self, error_text: str) -> ExtractedError:
        frames = []
        for match in self.FRAME_PATTERN.finditer(error_text):
            file_path, line_num, method = match.groups()
            frames.append(StackFrame(
                file_path=file_path,
                line_number=int(line_num),
                method_name=method
            ))

        exception_type, message = "Unknown", ""
        exc_match = self.EXCEPTION_PATTERN.search(error_text)
        if exc_match:
            exception_type, message = exc_match.groups()

        return ExtractedError(
            exception_type=exception_type,
            message=message,
            frames=frames,
            raw_text=error_text
        )
