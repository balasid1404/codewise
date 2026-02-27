#!/usr/bin/env python3
"""Generate dummy codebase with ~1000 files for testing scale."""

import random
from pathlib import Path

PACKAGES = ["service", "controller", "repository", "model", "util", "handler", "processor", "validator", "transformer", "client"]
DOMAINS = ["user", "order", "payment", "product", "inventory", "shipping", "notification", "auth", "report", "analytics"]

PYTHON_CLASS_TEMPLATE = '''"""Module for {domain} {package}."""

from typing import Optional, List


class {class_name}:
    """{class_name} handles {domain} {package} operations."""

    def __init__(self, config: dict = None):
        self.config = config or {{}}
        self._cache = {{}}

    def {method1}(self, {param1}: {type1}) -> {return1}:
        """Process {domain} data."""
        result = self._validate({param1})
        return self._transform(result)

    def {method2}(self, items: List[dict]) -> List[dict]:
        """Batch process {domain} items."""
        return [self.{method1}(item) for item in items]

    def _validate(self, data: {type1}) -> {type1}:
        """Validate input data."""
        if not data:
            raise ValueError("Invalid {domain} data")
        return data

    def _transform(self, data: {type1}) -> {return1}:
        """Transform validated data."""
        return data

    def {method3}(self, key: str) -> Optional[dict]:
        """Get cached {domain} by key."""
        return self._cache.get(key)

    def {method4}(self, key: str, value: dict) -> None:
        """Cache {domain} data."""
        self._cache[key] = value
'''

JAVA_CLASS_TEMPLATE = '''package com.example.{package};

import java.util.List;
import java.util.Map;
import java.util.HashMap;
import java.util.Optional;

/**
 * {class_name} handles {domain} operations.
 */
public class {class_name} {{
    private Map<String, Object> cache = new HashMap<>();
    private {dep_class} dependency;

    public {class_name}({dep_class} dependency) {{
        this.dependency = dependency;
    }}

    public {return_type} {method1}({param_type} {param_name}) {{
        validate({param_name});
        return transform({param_name});
    }}

    public List<{return_type}> {method2}(List<{param_type}> items) {{
        return items.stream()
            .map(this::{method1})
            .toList();
    }}

    private void validate({param_type} data) {{
        if (data == null) {{
            throw new IllegalArgumentException("Invalid {domain} data");
        }}
    }}

    private {return_type} transform({param_type} data) {{
        return data;
    }}

    public Optional<Object> {method3}(String key) {{
        return Optional.ofNullable(cache.get(key));
    }}

    public void {method4}(String key, Object value) {{
        cache.put(key, value);
    }}
}}
'''

def random_method_name(prefix: str) -> str:
    actions = ["process", "handle", "execute", "run", "compute", "fetch", "load", "save", "update", "delete"]
    return f"{random.choice(actions)}{prefix.title()}"

def generate_python_file(domain: str, package: str) -> str:
    class_name = f"{domain.title()}{package.title()}"
    return PYTHON_CLASS_TEMPLATE.format(
        domain=domain,
        package=package,
        class_name=class_name,
        method1=random_method_name(domain),
        method2=f"batch{random_method_name(domain).title()}",
        method3=f"get{domain.title()}",
        method4=f"cache{domain.title()}",
        param1="data",
        type1="dict",
        return1="dict"
    )

def generate_java_file(domain: str, package: str, dep_domain: str) -> str:
    class_name = f"{domain.title()}{package.title()}"
    dep_class = f"{dep_domain.title()}Repository"
    return JAVA_CLASS_TEMPLATE.format(
        domain=domain,
        package=package,
        class_name=class_name,
        dep_class=dep_class,
        method1=random_method_name(domain),
        method2=f"batch{random_method_name(domain).title()}",
        method3=f"get{domain.title()}",
        method4=f"cache{domain.title()}",
        param_type="Object",
        param_name="data",
        return_type="Object"
    )

def main():
    base = Path(__file__).parent.parent / "test-codebase"
    base.mkdir(exist_ok=True)

    count = 0

    # Generate Python files (~500)
    for domain in DOMAINS:
        for package in PACKAGES:
            for i in range(5):
                dir_path = base / "python" / domain / package
                dir_path.mkdir(parents=True, exist_ok=True)
                file_path = dir_path / f"{domain}_{package}_{i}.py"
                file_path.write_text(generate_python_file(domain, package))
                count += 1

    # Generate Java files (~500)
    for domain in DOMAINS:
        for package in PACKAGES:
            for i in range(5):
                dir_path = base / "java" / "com" / "example" / package / domain
                dir_path.mkdir(parents=True, exist_ok=True)
                dep_domain = random.choice([d for d in DOMAINS if d != domain])
                file_path = dir_path / f"{domain.title()}{package.title()}{i}.java"
                file_path.write_text(generate_java_file(domain, package, dep_domain))
                count += 1

    print(f"Generated {count} files in {base}")

if __name__ == "__main__":
    main()
