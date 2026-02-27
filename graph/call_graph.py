import networkx as nx
from indexer.entities import CodeEntity


class CallGraph:
    def __init__(self):
        self.graph = nx.DiGraph()

    def build(self, entities: list[CodeEntity]) -> None:
        # Add all entities as nodes
        for entity in entities:
            self.graph.add_node(entity.full_name, entity=entity)

        # Build name -> entity mapping for call resolution
        name_map: dict[str, list[CodeEntity]] = {}
        for entity in entities:
            name_map.setdefault(entity.name, []).append(entity)
            name_map.setdefault(entity.full_name, []).append(entity)

        # Add edges for calls
        for entity in entities:
            for call in entity.calls:
                targets = name_map.get(call, [])
                for target in targets:
                    self.graph.add_edge(entity.full_name, target.full_name)

    def get_callers(self, method_name: str, depth: int = 2) -> list[str]:
        """Get methods that call this method (upstream)."""
        if method_name not in self.graph:
            # Try partial match
            matches = [n for n in self.graph.nodes if n.endswith(method_name)]
            if not matches:
                return []
            method_name = matches[0]

        callers = set()
        current = {method_name}

        for _ in range(depth):
            next_level = set()
            for node in current:
                for pred in self.graph.predecessors(node):
                    if pred not in callers:
                        callers.add(pred)
                        next_level.add(pred)
            current = next_level

        return list(callers)

    def get_callees(self, method_name: str, depth: int = 2) -> list[str]:
        """Get methods this method calls (downstream)."""
        if method_name not in self.graph:
            matches = [n for n in self.graph.nodes if n.endswith(method_name)]
            if not matches:
                return []
            method_name = matches[0]

        callees = set()
        current = {method_name}

        for _ in range(depth):
            next_level = set()
            for node in current:
                for succ in self.graph.successors(node):
                    if succ not in callees:
                        callees.add(succ)
                        next_level.add(succ)
            current = next_level

        return list(callees)

    def get_related(self, method_name: str, depth: int = 2) -> list[str]:
        """Get both callers and callees."""
        callers = self.get_callers(method_name, depth)
        callees = self.get_callees(method_name, depth)
        return list(set(callers + callees))
