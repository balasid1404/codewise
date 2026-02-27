"""Tests for call graph."""

import pytest
from graph import CallGraph
from indexer.entities import CodeEntity, EntityType


def make_entity(name: str, calls: list[str], class_name: str = None) -> CodeEntity:
    return CodeEntity(
        id=name,
        name=name,
        entity_type=EntityType.METHOD if class_name else EntityType.FUNCTION,
        file_path="test.py",
        start_line=1,
        end_line=10,
        signature=f"def {name}()",
        body="pass",
        class_name=class_name,
        calls=calls
    )


class TestCallGraph:
    def test_build_graph(self):
        entities = [
            make_entity("main", ["process"]),
            make_entity("process", ["validate"]),
            make_entity("validate", []),
        ]
        graph = CallGraph()
        graph.build(entities)

        assert "main" in graph.graph.nodes
        assert graph.graph.has_edge("main", "process")

    def test_get_callers(self):
        entities = [
            make_entity("main", ["process"]),
            make_entity("process", ["validate"]),
            make_entity("validate", []),
        ]
        graph = CallGraph()
        graph.build(entities)

        callers = graph.get_callers("validate", depth=2)
        assert "process" in callers
        assert "main" in callers

    def test_get_callees(self):
        entities = [
            make_entity("main", ["process"]),
            make_entity("process", ["validate"]),
            make_entity("validate", []),
        ]
        graph = CallGraph()
        graph.build(entities)

        callees = graph.get_callees("main", depth=2)
        assert "process" in callees
        assert "validate" in callees
