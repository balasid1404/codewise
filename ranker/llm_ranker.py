import json
import boto3
from indexer.entities import CodeEntity
from extractors.base import ExtractedError


class LLMRanker:
    def __init__(self, model_id: str = "anthropic.claude-3-5-sonnet-20241022-v2:0"):
        self.client = boto3.client("bedrock-runtime")
        self.model_id = model_id

    def rank_and_explain(
        self,
        error: ExtractedError,
        candidates: list[tuple[CodeEntity, float]],
        top_k: int = 5
    ) -> list[dict]:
        prompt = self._build_prompt(error, candidates)

        response = self.client.invoke_model(
            modelId=self.model_id,
            body=json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 2000,
                "messages": [{"role": "user", "content": prompt}]
            })
        )

        result = json.loads(response["body"].read())
        content = result["content"][0]["text"]

        return self._parse_response(content, candidates, top_k)

    def _build_prompt(self, error: ExtractedError, candidates: list[tuple[CodeEntity, float]]) -> str:
        candidate_text = "\n\n".join([
            f"[{i}] {e.full_name} ({e.file_path}:{e.start_line})\n```\n{e.signature}\n{e.body[:500]}...\n```"
            for i, (e, _) in enumerate(candidates[:15])
        ])

        return f"""You are a fault localization expert. Given a stack trace and candidate code locations, rank the most likely root causes.

STACK TRACE:
{error.raw_text}

EXCEPTION: {error.exception_type}: {error.message}

CANDIDATE METHODS:
{candidate_text}

Respond with JSON array of top 5 most likely fault locations:
[
  {{"index": 0, "confidence": 0.9, "reason": "brief explanation"}},
  ...
]

Consider:
1. Methods directly in stack trace are symptoms, look for root causes
2. Methods that call stack trace methods may be the actual bug
3. Data validation/transformation methods are common fault sources
4. Look for methods handling the exception type"""

    def _parse_response(self, content: str, candidates: list[tuple[CodeEntity, float]], top_k: int) -> list[dict]:
        try:
            # Extract JSON from response
            start = content.find("[")
            end = content.rfind("]") + 1
            rankings = json.loads(content[start:end])

            results = []
            for r in rankings[:top_k]:
                idx = r.get("index", 0)
                if idx < len(candidates):
                    entity, score = candidates[idx]
                    results.append({
                        "entity": entity,
                        "retrieval_score": score,
                        "confidence": r.get("confidence", 0),
                        "reason": r.get("reason", "")
                    })
            return results
        except (json.JSONDecodeError, KeyError):
            # Fallback: return top candidates by retrieval score
            return [{"entity": e, "retrieval_score": s, "confidence": 0, "reason": ""} for e, s in candidates[:top_k]]
