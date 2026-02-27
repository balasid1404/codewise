from dataclasses import dataclass
from typing import Optional
from abc import ABC, abstractmethod


@dataclass
class StackFrame:
    file_path: str
    line_number: int
    method_name: str
    class_name: Optional[str] = None
    package: Optional[str] = None

    @property
    def full_method(self) -> str:
        if self.class_name:
            return f"{self.class_name}.{self.method_name}"
        return self.method_name


@dataclass
class ExtractedError:
    exception_type: str
    message: str
    frames: list[StackFrame]
    raw_text: str

    @property
    def file_paths(self) -> list[str]:
        return list(set(f.file_path for f in self.frames))

    @property
    def method_names(self) -> list[str]:
        return [f.full_method for f in self.frames]


class BaseExtractor(ABC):
    @abstractmethod
    def extract(self, error_text: str) -> ExtractedError:
        pass

    @abstractmethod
    def can_parse(self, error_text: str) -> bool:
        pass
