import os
import requests


class OpenRouterPostScript:
    """Post-script reasoning/visual planning using NVIDIA Nemotron 3 Ultra free."""

    URL = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(self):
        self.key = os.getenv("OPENROUTER_API_KEY", "").strip()
        self.model = os.getenv(
            "OPENROUTER_POST_SCRIPT_MODEL",
            "nvidia/nemotron-3-ultra-550b-a55b:free",
        )
        if not self.key:
            raise RuntimeError("OPENROUTER_API_KEY is required for post-script planning")

    def json(self, prompt, max_tokens=12000):
        response = requests.post(
            self.URL,
            headers={
                "Authorization": f"Bearer {self.key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/muneebdeveloper1/first",
                "X-Title": "Autonomous Audiobook Visual Planner",
            },
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": "Return ONLY valid JSON. No markdown fences."},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": max_tokens,
                "temperature": 0.2,
            },
            timeout=180,
        )
        if not response.ok:
            raise RuntimeError(f"OpenRouter HTTP {response.status_code}: {response.text[:1200]}")
        data = response.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        if not content:
            raise RuntimeError("OpenRouter returned empty output")
        text = content.strip()
        if text.startswith("```"):
            lines = text.splitlines()[1:]
            if lines and lines[-1].strip() == "```": lines = lines[:-1]
            text = "\n".join(lines).strip()
        import json
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            a, b = text.find("["), text.rfind("]")
            if a >= 0 and b > a:
                return json.loads(text[a:b + 1])
            a, b = text.find("{"), text.rfind("}")
            if a >= 0 and b > a:
                return json.loads(text[a:b + 1])
            raise RuntimeError(f"Nemotron returned invalid JSON: {text[:3000]}")
