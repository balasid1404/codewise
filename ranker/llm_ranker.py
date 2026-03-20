import json
import boto3
from indexer.entities import CodeEntity
from extractors.base import ExtractedError


class LLMRanker:
    def __init__(self, model_id: str = "amazon.nova-pro-v1:0", region: str = "us-east-1"):
        self.client = boto3.client("bedrock-runtime", region_name=region)
        self.model_id = model_id

    def rank_and_explain(
        self,
        error: ExtractedError,
        candidates: list[tuple[CodeEntity, float]],
        top_k: int = 5
    ) -> list[dict]:
        is_nl_query = error.exception_type in ("NLQuery", "Unknown") and not error.frames
        prompt = self._build_nl_prompt(error, candidates, top_k) if is_nl_query else self._build_prompt(error, candidates, top_k)

        body = {
            "messages": [{"role": "user", "content": [{"text": prompt}]}],
            "inferenceConfig": {"maxTokens": 2000}
        }

        response = self.client.converse(
            modelId=self.model_id,
            messages=body["messages"],
            inferenceConfig=body["inferenceConfig"]
        )

        content = response["output"]["message"]["content"][0]["text"]
        return self._parse_response(content, candidates, top_k)

    def _build_nl_prompt(self, error: ExtractedError, candidates: list[tuple[CodeEntity, float]], top_k: int = 5) -> str:
        candidate_text = "\n\n".join([
            f"[{i}] {e.full_name} ({e.file_path}:{e.start_line})\n```\n{e.signature}\n{e.body[:500]}...\n```"
            for i, (e, _) in enumerate(candidates[:max(15, top_k + 5)])
        ])

        return f"""You are a code navigation expert. A developer is asking a question about their codebase. Rank the candidate code locations by relevance to their question.

DEVELOPER QUESTION:
{error.raw_text}

CANDIDATE CODE LOCATIONS:
{candidate_text}

Respond with JSON array of top {top_k} most relevant code locations:
[
  {{"index": 0, "confidence": 0.9, "reason": "brief explanation of why this code is relevant"}},
  ...
]

Consider:
1. Which methods/classes directly implement the functionality the developer is asking about
2. Entry points and core logic are more useful than utility helpers
3. Prefer methods whose names and signatures clearly relate to the question
4. Consider the file path — it often reveals the module's purpose"""

    def _build_prompt(self, error: ExtractedError, candidates: list[tuple[CodeEntity, float]], top_k: int = 5) -> str:
        candidate_text = "\n\n".join([
            f"[{i}] {e.full_name} ({e.file_path}:{e.start_line})\n```\n{e.signature}\n{e.body[:500]}...\n```"
            for i, (e, _) in enumerate(candidates[:max(15, top_k + 5)])
        ])

        return f"""You are a fault localization expert. Given a stack trace and candidate code locations, rank the most likely root causes.

STACK TRACE:
{error.raw_text}

EXCEPTION: {error.exception_type}: {error.message}

CANDIDATE METHODS:
{candidate_text}

Respond with JSON array of top {top_k} most likely fault locations:
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
            return [{"entity": e, "retrieval_score": s, "confidence": s, "reason": ""} for e, s in candidates[:top_k]]
