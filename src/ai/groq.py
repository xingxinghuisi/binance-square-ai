from src.ai.provider import ProviderError
from src.http import HTTPClient


class GroqProvider:
    name = "groq"

    def __init__(self, http: HTTPClient, api_key: str, model: str, budget=None):
        self.http, self.api_key, self.model = http, api_key, model
        self.budget = budget

    async def generate(self, system: str, prompt: str, *, max_tokens: int = 900) -> str:
        result = await self.http.json(
            "POST", "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            before_attempt=self.budget,
            json={"model": self.model, "messages": [{"role": "system", "content": system},
                                                      {"role": "user", "content": prompt}],
                  "temperature": 0.6, "max_completion_tokens": max_tokens,
                  "response_format": {"type": "json_object"}},
        )
        try:
            choice = result["choices"][0]
            if choice.get("finish_reason") not in (None, "stop"):
                raise ProviderError("Groq response incomplete")
            text = choice["message"]["content"]
            if not isinstance(text, str) or not text.strip():
                raise ProviderError("Groq returned no text")
            return text
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError("Groq response invalid") from exc
