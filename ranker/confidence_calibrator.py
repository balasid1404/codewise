"""Confidence calibration for fault localization results."""

from dataclasses import dataclass
from typing import Optional


@dataclass
class CalibrationFactors:
    """Factors that affect confidence."""
    stack_trace_match: float = 0.0  # Direct match in stack trace
    call_graph_distance: int = 0     # Hops from stack trace
    semantic_score: float = 0.0      # Embedding similarity
    bm25_score: float = 0.0          # Keyword match
    file_path_match: bool = False    # File mentioned in error
    method_name_match: bool = False  # Method mentioned in error


class ConfidenceCalibrator:
    """Calibrate confidence scores based on multiple signals."""

    # Weights for different signals
    WEIGHTS = {
        "stack_trace_match": 0.35,
        "call_graph_proximity": 0.20,
        "semantic_similarity": 0.25,
        "keyword_match": 0.15,
        "context_match": 0.05
    }

    def calibrate(self, factors: CalibrationFactors) -> float:
        """
        Calculate calibrated confidence score.
        
        Returns: 0.0 - 1.0 confidence score
        """
        score = 0.0

        # Stack trace direct match (highest signal)
        if factors.stack_trace_match > 0:
            score += self.WEIGHTS["stack_trace_match"] * factors.stack_trace_match

        # Call graph proximity (closer = higher)
        if factors.call_graph_distance >= 0:
            proximity = max(0, 1 - (factors.call_graph_distance * 0.2))
            score += self.WEIGHTS["call_graph_proximity"] * proximity

        # Semantic similarity
        score += self.WEIGHTS["semantic_similarity"] * factors.semantic_score

        # BM25/keyword match
        score += self.WEIGHTS["keyword_match"] * min(1.0, factors.bm25_score)

        # Context matches (file/method name in error)
        context_score = 0.0
        if factors.file_path_match:
            context_score += 0.5
        if factors.method_name_match:
            context_score += 0.5
        score += self.WEIGHTS["context_match"] * context_score

        return min(1.0, max(0.0, score))

    def get_confidence_label(self, score: float) -> str:
        """Get human-readable confidence label."""
        if score >= 0.8:
            return "high"
        elif score >= 0.5:
            return "medium"
        elif score >= 0.3:
            return "low"
        else:
            return "very_low"
