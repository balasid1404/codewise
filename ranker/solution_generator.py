"""Generate concrete solutions from fault candidates using LLM."""

import json
import boto3
from pathlib import Path
from indexer.entities import CodeEntity


class SolutionGenerator:
    """Analyzes fault candidates and generates concrete fix suggestions."""

    def __init__(self, model_id: str = "us.anthropic.claude-3-5-sonnet-20241022-v2:0", region: str = "us-east-1"):
        self.client = boto3.client("bedrock-runtime", region_name=region)
        self.model_id = model_id

    def generate_solution(
        self,
        error_description: str,
        candidates: list[dict],
        codebase_path: str = None
    ) -> dict:
        """
        Analyze candidates and generate concrete solution.
        
        Args:
            error_description: The bug/error description
            candidates: List of candidate dicts with 'entity' key
            codebase_path: Optional path to read full file contents
            
        Returns:
            dict with diagnosis, root_cause, solution, and code_fix
        """
        # Get full file contents for each candidate
        file_contents = self._get_file_contents(candidates, codebase_path)
        
        prompt = self._build_solution_prompt(error_description, candidates, file_contents)
        
        response = self.client.invoke_model(
            modelId=self.model_id,
            body=json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 4000,
                "messages": [{"role": "user", "content": prompt}]
            })
        )
        
        result = json.loads(response["body"].read())
        content = result["content"][0]["text"]
        
        return self._parse_solution(content)

    def _get_file_contents(self, candidates: list[dict], codebase_path: str) -> dict[str, str]:
        """Read full file contents for candidate files."""
        file_contents = {}
        
        for candidate in candidates[:5]:  # Top 5 only
            entity = candidate.get("entity")
            if not entity:
                continue
                
            file_path = entity.file_path
            if file_path in file_contents:
                continue
                
            try:
                # Try absolute path first
                if Path(file_path).exists():
                    content = Path(file_path).read_text()
                elif codebase_path:
                    # Try relative to codebase
                    full_path = Path(codebase_path) / file_path
                    if full_path.exists():
                        content = full_path.read_text()
                    else:
                        content = None
                else:
                    content = None
                    
                if content:
                    file_contents[file_path] = content
            except Exception:
                pass
                
        return file_contents

    def _build_solution_prompt(
        self,
        error_description: str,
        candidates: list[dict],
        file_contents: dict[str, str]
    ) -> str:
        # Build candidate summaries
        candidate_summaries = []
        for i, c in enumerate(candidates[:5], 1):
            entity = c.get("entity")
            if not entity:
                continue
            candidate_summaries.append(
                f"{i}. {entity.full_name}\n"
                f"   File: {entity.file_path}:{entity.start_line}-{entity.end_line}\n"
                f"   Signature: {entity.signature}\n"
                f"   Reason flagged: {c.get('reason', 'retrieval match')}"
            )
        
        # Build file contents section
        files_section = ""
        for path, content in file_contents.items():
            # Truncate very long files
            if len(content) > 8000:
                content = content[:8000] + "\n... [truncated]"
            files_section += f"\n\n=== {path} ===\n```\n{content}\n```"
        
        return f"""You are a senior software engineer debugging a production issue.

## BUG REPORT
{error_description}

## SUSPECTED FAULT LOCATIONS (from automated analysis)
{chr(10).join(candidate_summaries)}

## RELEVANT SOURCE FILES
{files_section}

## YOUR TASK
Analyze the bug and the suspected locations. Provide:

1. **DIAGNOSIS**: What is actually causing this bug? Be specific.

2. **ROOT CAUSE**: Which file and method is the actual root cause? (may or may not be in the candidates)

3. **SOLUTION**: How to fix it - explain the logic change needed.

4. **CODE FIX**: Provide the exact code change needed. Use this format:
   ```
   FILE: <filepath>
   BEFORE:
   <original code>
   AFTER:
   <fixed code>
   ```

5. **CONFIDENCE**: How confident are you (0-100%) and why?

Be concrete and actionable. If you need more context, say what files you'd need to see."""

    def _parse_solution(self, content: str) -> dict:
        """Parse LLM response into structured solution."""
        return {
            "raw_analysis": content,
            "diagnosis": self._extract_section(content, "DIAGNOSIS"),
            "root_cause": self._extract_section(content, "ROOT CAUSE"),
            "solution": self._extract_section(content, "SOLUTION"),
            "code_fix": self._extract_section(content, "CODE FIX"),
            "confidence": self._extract_section(content, "CONFIDENCE")
        }

    def _extract_section(self, content: str, section_name: str) -> str:
        """Extract a section from the response."""
        import re
        
        # Try to find section with ** markers or # markers
        patterns = [
            rf"\*\*{section_name}\*\*:?\s*(.*?)(?=\*\*[A-Z]|\Z)",
            rf"#{1,3}\s*{section_name}:?\s*(.*?)(?=#{1,3}\s*[A-Z]|\Z)",
            rf"{section_name}:?\s*(.*?)(?=[A-Z][A-Z]+:|\Z)"
        ]
        
        for pattern in patterns:
            match = re.search(pattern, content, re.DOTALL | re.IGNORECASE)
            if match:
                return match.group(1).strip()
        
        return ""
