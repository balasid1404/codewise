"""Tests for stack trace extractors."""

import pytest
from extractors import PythonStackExtractor, JavaStackExtractor


class TestPythonExtractor:
    def setup_method(self):
        self.extractor = PythonStackExtractor()

    def test_can_parse_python_traceback(self):
        error = """Traceback (most recent call last):
  File "app.py", line 10, in main
    foo()
ValueError: bad value"""
        assert self.extractor.can_parse(error)

    def test_cannot_parse_java(self):
        error = """java.lang.NullPointerException
    at com.example.Main.run(Main.java:10)"""
        assert not self.extractor.can_parse(error)

    def test_extract_frames(self):
        error = """Traceback (most recent call last):
  File "app/main.py", line 42, in handle
    process()
  File "app/processor.py", line 10, in process
    validate()
ValueError: invalid"""
        result = self.extractor.extract(error)

        assert len(result.frames) == 2
        assert result.frames[0].file_path == "app/main.py"
        assert result.frames[0].line_number == 42
        assert result.frames[0].method_name == "handle"
        assert result.exception_type == "ValueError"
        assert result.message == "invalid"


class TestJavaExtractor:
    def setup_method(self):
        self.extractor = JavaStackExtractor()

    def test_can_parse_java_stacktrace(self):
        error = """java.lang.NullPointerException: msg
    at com.example.Service.run(Service.java:45)"""
        assert self.extractor.can_parse(error)

    def test_extract_frames(self):
        error = """java.lang.IllegalArgumentException: bad arg
    at com.example.service.UserService.getUser(UserService.java:45)
    at com.example.controller.Controller.handle(Controller.java:23)"""
        result = self.extractor.extract(error)

        assert len(result.frames) == 2
        assert result.frames[0].class_name == "UserService"
        assert result.frames[0].method_name == "getUser"
        assert result.frames[0].package == "com.example.service"
        assert result.exception_type == "java.lang.IllegalArgumentException"
