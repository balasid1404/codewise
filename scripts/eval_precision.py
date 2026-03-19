#!/usr/bin/env python3
"""
Automated precision/recall evaluator for CodeWise fault localization.

Evaluates how precisely the tool identifies files and code entities
that need changes for a given task. Compares tool output against
human-curated ground truth.

Usage:
    python scripts/eval_precision.py --config benchmarks/rename_task.json
    python scripts/eval_precision.py --config benchmarks/  # run all benchmarks
    python scripts/eval_precision.py --list                 # list available benchmarks
"""

import json
import sys
import time
import argparse
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# Ground truth & result models
# ---------------------------------------------------------------------------

@dataclass
class EntityChange:
    """A single expected change within a file."""
    entity_name: str                    # e.g. "HAWKFIRE_ALL_DEVICES_ANNUAL_NON_DISCOUNTED"
    change_type: str                    # "rename" | "update_value" | "add" | "remove" | "update_mapping"
    description: str = ""               # human-readable description
    context: Optional[str] = None       # e.g. "PLAN_NAME_TO_TYPE_MAP"


@dataclass
class FileChange:
    """Expected changes in a single file."""
    file_path: str                      # relative path from repo root
    entities: list[EntityChange] = field(default_factory=list)


@dataclass
class BenchmarkTask:
    """A complete benchmark task with ground truth."""
    id: str
    name: str
    description: str                    # the natural language task description
    query: str                          # what to feed the localizer
    repo_paths: list[str]              # codebase paths to index
    namespace: Optional[str] = None
    ground_truth: list[FileChange] = field(default_factory=list)

    @property
    def expected_files(self) -> set[str]:
        return {fc.file_path for fc in self.ground_truth}

    @property
    def expected_entities(self) -> set[str]:
        entities = set()
        for fc in self.ground_truth:
            for ec in fc.entities:
                entities.add(f"{fc.file_path}::{ec.entity_name}")
        return entities


@dataclass
class EvalResult:
    """Evaluation result for a single benchmark task."""
    task_id: str
    task_name: str

    # File-level metrics
    file_precision: float = 0.0
    file_recall: float = 0.0
    file_f1: float = 0.0
    files_expected: list[str] = field(default_factory=list)
    files_predicted: list[str] = field(default_factory=list)
    files_true_positive: list[str] = field(default_factory=list)
    files_false_positive: list[str] = field(default_factory=list)
    files_false_negative: list[str] = field(default_factory=list)

    # Entity-level metrics
    entity_precision: float = 0.0
    entity_recall: float = 0.0
    entity_f1: float = 0.0
    entities_expected: list[str] = field(default_factory=list)
    entities_predicted: list[str] = field(default_factory=list)
    entities_true_positive: list[str] = field(default_factory=list)
    entities_false_positive: list[str] = field(default_factory=list)
    entities_false_negative: list[str] = field(default_factory=list)

    # Ranking metrics
    top_1_file_hit: bool = False
    top_3_file_hit: bool = False
    top_5_file_hit: bool = False
    mean_reciprocal_rank: float = 0.0

    # Timing
    index_time_s: float = 0.0
    query_time_s: float = 0.0
    total_time_s: float = 0.0

    # Raw output
    top_k_used: int = 0
    raw_results: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Evaluation engine
# ---------------------------------------------------------------------------

class PrecisionEvaluator:
    """Runs benchmark tasks and computes precision/recall metrics."""

    def __init__(self, use_llm: bool = True, top_k: int = 10):
        self.use_llm = use_llm
        self.top_k = top_k

    def load_task(self, config_path: str) -> BenchmarkTask:
        """Load a benchmark task from JSON config."""
        data = json.loads(Path(config_path).read_text())
        ground_truth = []
        for fc_data in data.get("ground_truth", []):
            entities = [EntityChange(**ec) for ec in fc_data.get("entities", [])]
            ground_truth.append(FileChange(
                file_path=fc_data["file_path"],
                entities=entities
            ))
        return BenchmarkTask(
            id=data["id"],
            name=data["name"],
            description=data["description"],
            query=data["query"],
            repo_paths=data["repo_paths"],
            namespace=data.get("namespace"),
            ground_truth=ground_truth
        )

    def evaluate(self, task: BenchmarkTask) -> EvalResult:
        """Run the localizer on a task and compute metrics."""
        from fault_localizer_prod import FaultLocalizerProd

        result = EvalResult(
            task_id=task.id,
            task_name=task.name,
            files_expected=sorted(task.expected_files),
            entities_expected=sorted(task.expected_entities),
            top_k_used=self.top_k,
        )

        localizer = FaultLocalizerProd(use_llm=self.use_llm)

        # Index
        t0 = time.time()
        total_entities = 0
        for repo_path in task.repo_paths:
            ns = task.namespace or Path(repo_path).name
            count = localizer.index_codebase(repo_path, namespace=ns)
            total_entities += count
        result.index_time_s = time.time() - t0

        # Query
        t1 = time.time()
        raw = localizer.localize(task.query, top_k=self.top_k, namespace=task.namespace)
        result.query_time_s = time.time() - t1
        result.total_time_s = result.index_time_s + result.query_time_s

        # Extract predicted files and entities from results
        predicted_files = []
        predicted_entities = []
        for r in raw:
            entity = r.get("entity")
            if entity is None:
                continue
            fp = getattr(entity, "file_path", None) or entity.get("file_path", "")
            name = getattr(entity, "name", None) or entity.get("name", "")
            if fp and fp not in predicted_files:
                predicted_files.append(fp)
            if fp and name:
                eid = f"{fp}::{name}"
                if eid not in predicted_entities:
                    predicted_entities.append(eid)

        result.files_predicted = predicted_files
        result.entities_predicted = predicted_entities

        # Store raw results for inspection
        result.raw_results = _serialize_results(raw)

        # Compute file-level metrics
        expected_files = task.expected_files
        pred_files_set = set(predicted_files)

        tp_files = expected_files & pred_files_set
        fp_files = pred_files_set - expected_files
        fn_files = expected_files - pred_files_set

        result.files_true_positive = sorted(tp_files)
        result.files_false_positive = sorted(fp_files)
        result.files_false_negative = sorted(fn_files)

        if pred_files_set:
            result.file_precision = len(tp_files) / len(pred_files_set)
        if expected_files:
            result.file_recall = len(tp_files) / len(expected_files)
        if result.file_precision + result.file_recall > 0:
            result.file_f1 = (2 * result.file_precision * result.file_recall /
                              (result.file_precision + result.file_recall))

        # Compute entity-level metrics
        expected_entities = task.expected_entities
        pred_entities_set = set(predicted_entities)

        # Fuzzy entity matching: predicted "PlanName" matches expected
        # "PlanName.java::HAWKFIRE_ALL_DEVICES_ANNUAL_NON_DISCOUNTED" if entity name is substring
        tp_entities = set()
        for pred in pred_entities_set:
            pred_name = pred.split("::")[-1] if "::" in pred else pred
            for exp in expected_entities:
                exp_name = exp.split("::")[-1] if "::" in exp else exp
                if pred_name == exp_name or pred_name in exp_name or exp_name in pred_name:
                    tp_entities.add(exp)

        fp_entities = pred_entities_set - {p for p in pred_entities_set
                                           if any(p.split("::")[-1] in e or e.split("::")[-1] in p
                                                  for e in expected_entities)}
        fn_entities = expected_entities - tp_entities

        result.entities_true_positive = sorted(tp_entities)
        result.entities_false_positive = sorted(fp_entities)
        result.entities_false_negative = sorted(fn_entities)

        if pred_entities_set:
            result.entity_precision = len(tp_entities) / len(pred_entities_set)
        if expected_entities:
            result.entity_recall = len(tp_entities) / len(expected_entities)
        if result.entity_precision + result.entity_recall > 0:
            result.entity_f1 = (2 * result.entity_precision * result.entity_recall /
                                (result.entity_precision + result.entity_recall))

        # Ranking metrics (file-level)
        for i, fp in enumerate(predicted_files):
            if fp in expected_files:
                result.mean_reciprocal_rank = 1.0 / (i + 1)
                if i == 0:
                    result.top_1_file_hit = True
                if i < 3:
                    result.top_3_file_hit = True
                if i < 5:
                    result.top_5_file_hit = True
                break

        return result

    def evaluate_batch(self, config_dir: str) -> list[EvalResult]:
        """Run all benchmark tasks in a directory."""
        results = []
        config_path = Path(config_dir)
        configs = sorted(config_path.glob("*.json"))
        for cfg in configs:
            print(f"\n{'='*60}")
            print(f"Running: {cfg.stem}")
            print(f"{'='*60}")
            task = self.load_task(str(cfg))
            result = self.evaluate(task)
            results.append(result)
            _print_result(result)
        return results


def _serialize_results(raw: list[dict]) -> list[dict]:
    """Serialize localizer results for JSON storage."""
    serialized = []
    for r in raw:
        entry = {}
        entity = r.get("entity")
        if entity is not None:
            if hasattr(entity, "file_path"):
                entry["file_path"] = entity.file_path
                entry["name"] = entity.name
                entry["full_name"] = entity.full_name
                entry["entity_type"] = entity.entity_type.value if hasattr(entity.entity_type, "value") else str(entity.entity_type)
                entry["start_line"] = entity.start_line
                entry["end_line"] = entity.end_line
            else:
                entry.update({k: v for k, v in entity.items() if k != "embedding"})
        entry["score"] = r.get("score", 0)
        entry["confidence"] = r.get("confidence", 0)
        entry["reason"] = r.get("reason", "")
        serialized.append(entry)
    return serialized


def _print_result(result: EvalResult) -> None:
    """Pretty-print a single evaluation result."""
    print(f"\n--- {result.task_name} ---")
    print(f"  Index time:  {result.index_time_s:.1f}s")
    print(f"  Query time:  {result.query_time_s:.1f}s")

    print(f"\n  FILE-LEVEL:")
    print(f"    Precision: {result.file_precision:.2%}")
    print(f"    Recall:    {result.file_recall:.2%}")
    print(f"    F1:        {result.file_f1:.2%}")
    print(f"    TP: {result.files_true_positive}")
    print(f"    FP: {result.files_false_positive}")
    print(f"    FN: {result.files_false_negative}")

    print(f"\n  ENTITY-LEVEL:")
    print(f"    Precision: {result.entity_precision:.2%}")
    print(f"    Recall:    {result.entity_recall:.2%}")
    print(f"    F1:        {result.entity_f1:.2%}")
    if result.entities_true_positive:
        print(f"    TP: {result.entities_true_positive}")
    if result.entities_false_positive:
        print(f"    FP: {result.entities_false_positive}")
    if result.entities_false_negative:
        print(f"    FN: {result.entities_false_negative}")

    print(f"\n  RANKING:")
    print(f"    Top-1 hit: {result.top_1_file_hit}")
    print(f"    Top-3 hit: {result.top_3_file_hit}")
    print(f"    Top-5 hit: {result.top_5_file_hit}")
    print(f"    MRR:       {result.mean_reciprocal_rank:.3f}")


def _print_summary(results: list[EvalResult]) -> None:
    """Print aggregate summary across all tasks."""
    n = len(results)
    if n == 0:
        print("No results.")
        return

    avg = lambda vals: sum(vals) / len(vals) if vals else 0

    print(f"\n{'='*60}")
    print(f"AGGREGATE SUMMARY ({n} tasks)")
    print(f"{'='*60}")
    print(f"  File Precision:  {avg([r.file_precision for r in results]):.2%}")
    print(f"  File Recall:     {avg([r.file_recall for r in results]):.2%}")
    print(f"  File F1:         {avg([r.file_f1 for r in results]):.2%}")
    print(f"  Entity Precision:{avg([r.entity_precision for r in results]):.2%}")
    print(f"  Entity Recall:   {avg([r.entity_recall for r in results]):.2%}")
    print(f"  Entity F1:       {avg([r.entity_f1 for r in results]):.2%}")
    print(f"  Top-1 hit rate:  {sum(r.top_1_file_hit for r in results)}/{n}")
    print(f"  Top-3 hit rate:  {sum(r.top_3_file_hit for r in results)}/{n}")
    print(f"  Top-5 hit rate:  {sum(r.top_5_file_hit for r in results)}/{n}")
    print(f"  Avg MRR:         {avg([r.mean_reciprocal_rank for r in results]):.3f}")
    print(f"  Avg query time:  {avg([r.query_time_s for r in results]):.2f}s")


def main():
    parser = argparse.ArgumentParser(description="Evaluate fault localization precision")
    parser.add_argument("--config", type=str, help="Path to benchmark JSON or directory of JSONs")
    parser.add_argument("--list", action="store_true", help="List available benchmarks")
    parser.add_argument("--top-k", type=int, default=10, help="Number of results to evaluate")
    parser.add_argument("--no-llm", action="store_true", help="Disable LLM reranking")
    parser.add_argument("--output", type=str, help="Save results to JSON file")
    args = parser.parse_args()

    benchmarks_dir = Path(__file__).parent.parent / "benchmarks"

    if args.list:
        if not benchmarks_dir.exists():
            print("No benchmarks directory found.")
            return
        for f in sorted(benchmarks_dir.glob("*.json")):
            data = json.loads(f.read_text())
            print(f"  {f.stem}: {data.get('name', 'unnamed')}")
        return

    if not args.config:
        parser.print_help()
        return

    evaluator = PrecisionEvaluator(use_llm=not args.no_llm, top_k=args.top_k)
    config_path = Path(args.config)

    if config_path.is_dir():
        results = evaluator.evaluate_batch(str(config_path))
    else:
        task = evaluator.load_task(str(config_path))
        result = evaluator.evaluate(task)
        results = [result]
        _print_result(result)

    _print_summary(results)

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps([asdict(r) for r in results], indent=2, default=str))
        print(f"\nResults saved to {args.output}")


if __name__ == "__main__":
    main()
