from urllib.parse import quote

from src.ai.provider import ProviderError
from src.http import HTTPClient


class GeminiProvider:
    name = "gemini"

    def __init__(self, http: HTTPClient, api_key: str, model: str, budget=None):
        self.http, self.api_key, self.model = http, api_key, model
        self.budget = budget

    async def generate(self, system: str, prompt: str, *, max_tokens: int = 900) -> str:
        result = await self.http.json(
            "POST", f"https://generativelanguage.googleapis.com/v1beta/models/{quote(self.model, safe='')}:generateContent",
            headers={"x-goog-api-key": self.api_key},
            before_attempt=self.budget,
            json={"systemInstruction": {"parts": [{"text": system}]},
                  "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                  "generationConfig": {"temperature": 0.6, "maxOutputTokens": max_tokens,
                                       "responseMimeType": "application/json"}},
        )
        try:
            candidate = result["candidates"][0]
            if candidate.get("finishReason") not in (None, "STOP"):
                raise ProviderError("Gemini response incomplete or blocked")
            text = "".join(part.get("text", "") for part in candidate["content"]["parts"] if not part.get("thought"))
            if not text.strip():
                raise ProviderError("Gemini returned no text")
            return text
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError("Gemini response invalid or blocked") from exc
