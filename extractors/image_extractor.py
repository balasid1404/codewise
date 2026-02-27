"""Extract fault context from screenshots."""

import base64
import json
import re
import boto3


class ImageExtractor:
    """Extract error context from bug screenshots using vision LLM."""

    def __init__(self, model_id: str = "anthropic.claude-3-5-sonnet-20241022-v2:0"):
        self.client = boto3.client("bedrock-runtime")
        self.model_id = model_id

    def extract_from_image(self, image_path: str) -> dict:
        """
        Extract fault-relevant information from a screenshot.
        
        Returns:
            {
                "error_message": str,      # Any visible error text
                "ui_elements": list[str],  # Button labels, screen titles, etc.
                "app_section": str,        # e.g., "payment", "checkout", "login"
                "user_action": str,        # What the user was trying to do
                "keywords": list[str],     # Search terms for code lookup
                "raw_text": str            # All visible text
            }
        """
        with open(image_path, "rb") as f:
            image_data = base64.b64encode(f.read()).decode()

        # Determine media type
        if image_path.endswith(".png"):
            media_type = "image/png"
        elif image_path.endswith(".jpg") or image_path.endswith(".jpeg"):
            media_type = "image/jpeg"
        else:
            media_type = "image/png"

        prompt = """Analyze this screenshot of a bug/error in an application.

Extract the following information as JSON:
{
    "error_message": "any error message or alert text visible",
    "ui_elements": ["list of button labels", "screen titles", "menu items visible"],
    "app_section": "which part of the app (e.g., payment, checkout, profile, search)",
    "user_action": "what the user was likely trying to do",
    "keywords": ["technical terms", "feature names", "that could help find relevant code"],
    "raw_text": "all readable text in the image"
}

Focus on information that would help a developer find the relevant code.
If no error is visible, describe the UI state that appears broken."""

        response = self.client.invoke_model(
            modelId=self.model_id,
            body=json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 1500,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_data}},
                        {"type": "text", "text": prompt}
                    ]
                }]
            })
        )

        result = json.loads(response["body"].read())
        content = result["content"][0]["text"]

        # Parse JSON from response
        try:
            start = content.find("{")
            end = content.rfind("}") + 1
            return json.loads(content[start:end])
        except (json.JSONDecodeError, ValueError):
            return {
                "error_message": "",
                "ui_elements": [],
                "app_section": "unknown",
                "user_action": "unknown",
                "keywords": [],
                "raw_text": content
            }

    def build_search_query(self, extracted: dict) -> str:
        """Build a search query from extracted image data."""
        parts = []

        if extracted.get("error_message"):
            parts.append(extracted["error_message"])

        if extracted.get("app_section"):
            parts.append(extracted["app_section"])

        if extracted.get("keywords"):
            parts.extend(extracted["keywords"])

        if extracted.get("ui_elements"):
            # Add likely code-related UI elements
            for elem in extracted["ui_elements"]:
                # Convert UI text to potential code names
                # "Pay Now" -> "pay now paynow"
                parts.append(elem.lower())
                parts.append(elem.lower().replace(" ", ""))
                parts.append(elem.lower().replace(" ", "_"))

        return " ".join(parts)
