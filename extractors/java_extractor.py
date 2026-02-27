import re
from .base import BaseExtractor, StackFrame, ExtractedError


class JavaStackExtractor(BaseExtractor):
    # Pattern: at com.package.Class.method(File.java:42)
    FRAME_PATTERN = re.compile(
        r'at ([\w.$]+)\.([\w<>]+)\(([^:]+):(\d+)\)'
    )
    # Pattern: com.package.ExceptionType: message
    EXCEPTION_PATTERN = re.compile(
        r'^([\w.]+(?:Exception|Error|Throwable)): (.+)$', re.MULTILINE
    )
    # Detect Java stack trace
    JAVA_MARKER = re.compile(r'\tat [\w.$]+\.\w+\(')

    def can_parse(self, error_text: str) -> bool:
        return bool(self.JAVA_MARKER.search(error_text))

    def extract(self, error_text: str) -> ExtractedError:
        frames = []
        for match in self.FRAME_PATTERN.finditer(error_text):
            full_class, method, file_name, line_num = match.groups()
            parts = full_class.rsplit('.', 1)
            package = parts[0] if len(parts) > 1 else None
            class_name = parts[-1]

            frames.append(StackFrame(
                file_path=file_name,
                line_number=int(line_num),
                method_name=method,
                class_name=class_name,
                package=package
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
