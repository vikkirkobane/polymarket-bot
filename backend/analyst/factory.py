"""backend/analyst/factory.py — AI analyst implementations + factory."""

import asyncio
import anthropic
import openai as openai_lib
import logging

from backend.analyst.base import BaseAnalyst

logger = logging.getLogger(__name__)


# ── Claude (Anthropic) ─────────────────────────────────────────────────────────

class ClaudeAnalyst(BaseAnalyst):
    model_name = "claude"

    def __init__(self, api_key: str, model: str = "claude-3-5-sonnet-20241022"):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.model = model

    async def _call_model(self, prompt: str) -> str:
        message = await self.client.messages.create(
            model=self.model,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text


# ── OpenAI (GPT-4o) ────────────────────────────────────────────────────────────

class OpenAIAnalyst(BaseAnalyst):
    model_name = "openai"

    def __init__(self, api_key: str, model: str = "gpt-4o"):
        self.client = openai_lib.AsyncOpenAI(api_key=api_key)
        self.model = model

    async def _call_model(self, prompt: str) -> str:
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=4096,
            response_format={"type": "json_object"},
        )
        # gpt-4o with json_object might wrap in {"opportunities": [...]}
        text = response.choices[0].message.content
        # Unwrap if needed
        import json
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                # Return first list value found
                for v in parsed.values():
                    if isinstance(v, list):
                        return json.dumps(v)
        except Exception:
            pass
        return text


# ── Gemini (Google - 2026 SDK) ─────────────────────────────────────────────────

class GeminiAnalyst(BaseAnalyst):
    model_name = "gemini"

    def __init__(self, api_key: str, model: str = "gemini-3-flash"):
        from google import genai
        self.client = genai.Client(api_key=api_key, http_options={'api_version': 'v1'})
        self.model_id = model

    async def _call_model(self, prompt: str) -> str:
        # Fallback sequence for 2026 models
        fallbacks = [self.model_id, "gemini-3-flash", "gemini-3.1-flash-lite", "gemini-3.1-pro"]

        last_err = None
        for model_name in fallbacks:
            try:
                # Wrap synchronous SDK call in a thread to avoid blocking the event loop
                response = await asyncio.to_thread(
                    self.client.models.generate_content,
                    model=model_name,
                    contents=prompt,
                )
                if response and response.text:
                    if model_name != self.model_id:
                        logger.info(f"Switched to working Gemini model: {model_name}")
                        self.model_id = model_name
                    return response.text
            except Exception as e:
                last_err = e
                if "404" in str(e) or "INVALID_ARGUMENT" in str(e):
                    continue
                raise

        logger.error(f"Gemini API failed after fallbacks. Check your API key. Last error: {last_err}")
        raise last_err


# ── Mock (for testing) ─────────────────────────────────────────────────────────

class MockAnalyst(BaseAnalyst):
    model_name = "mock"

    async def _call_model(self, prompt: str) -> str:
        import json, random
        # Extract market_ids from prompt context
        markets = []
        import re
        ids = re.findall(r'"market_id": "([^"]+)"', prompt)
        questions = re.findall(r'"question": "([^"]+)"', prompt)
        for i, mid in enumerate(ids[:3]):
            edge = round(random.uniform(5, 20), 1)
            markets.append({
                "market_id": mid,
                "question": questions[i] if i < len(questions) else "Unknown market",
                "yes_price": 0.6,
                "no_price": 0.4,
                "recommended_side": random.choice(["YES", "NO"]),
                "edge_pct": edge,
                "confidence": random.choice(["low", "medium", "high"]),
                "reasoning": f"Mock analysis: {edge}% edge detected based on simulated data.",
            })
        return json.dumps(markets)


# ── OpenRouter ─────────────────────────────────────────────────────────────────

class OpenRouterAnalyst(BaseAnalyst):
    model_name = "openrouter"

    def __init__(self, api_key: str, model: str = "tencent/hy3-preview:free"):
        # OpenRouter is OpenAI-compatible
        self.client = openai_lib.AsyncOpenAI(
            api_key=api_key,
            base_url="https://openrouter.ai/api/v1",
        )
        self.model = model
        self._extra_headers = {
            "HTTP-Referer": "https://github.com/polymarket-bot", # For OpenRouter rankings
            "X-Title": "Polymarket Bot",
        }

    async def _call_model(self, prompt: str) -> str:
        # Ordered fallback list — tries each model in sequence if the previous is unavailable.
        fallbacks = [
            self.model,
            "openai/gpt-oss-120b:free",
            "google/gemma-4-31b-it:free",
            "nvidia/nemotron-3-super-120b-a12b:free",
            "inclusionai/ring-2.6-1t:free",
            "z-ai/glm-4.5-air:free",
            "openai/gpt-oss-20b:free",
            "qwen/qwen3-next-80b-a3b-instruct:free",
        ]
        # Deduplicate while preserving order
        seen: set = set()
        ordered = [m for m in fallbacks if not (m in seen or seen.add(m))]

        last_err = None
        for model_name in ordered:
            try:
                response = await self.client.chat.completions.create(
                    model=model_name,
                    messages=[{"role": "user", "content": prompt}],
                    extra_headers=self._extra_headers,
                    max_tokens=4096,
                )
                content = response.choices[0].message.content if response.choices else None
                if content:
                    if model_name != self.model:
                        logger.info(f"OpenRouter: switched to {model_name}")
                        self.model = model_name
                    return content
                logger.warning(f"OpenRouter {model_name} returned empty content, trying next")
            except Exception as e:
                last_err = e
                err_str = str(e)
                if any(code in err_str for code in ("404", "503", "502", "529")):
                    logger.warning(f"OpenRouter {model_name} unavailable ({err_str[:60]}), trying next")
                    continue
                if "429" in err_str:
                    logger.warning(f"OpenRouter {model_name} rate-limited, trying next")
                    continue
                raise

        logger.error(f"All OpenRouter models failed. Last error: {last_err}")
        raise last_err or RuntimeError("All OpenRouter fallback models exhausted")


# ── Factory ────────────────────────────────────────────────────────────────────

def get_analyst(model: str, settings) -> BaseAnalyst:
    """Return the appropriate analyst based on AI_MODEL setting."""
    model = model.lower().strip()
    if model == "claude":
        if not settings.anthropic_api_key:
            raise ValueError("ANTHROPIC_API_KEY is not set")
        return ClaudeAnalyst(api_key=settings.anthropic_api_key)
    elif model == "openai":
        if not settings.openai_api_key:
            raise ValueError("OPENAI_API_KEY is not set")
        return OpenAIAnalyst(api_key=settings.openai_api_key)
    elif model == "gemini":
        if not settings.google_api_key:
            raise ValueError("GOOGLE_API_KEY is not set")
        return GeminiAnalyst(api_key=settings.google_api_key)
    elif model == "openrouter":
        if not settings.openrouter_api_key:
            raise ValueError("OPENROUTER_API_KEY is not set")
        return OpenRouterAnalyst(api_key=settings.openrouter_api_key, model=settings.openrouter_model)
    elif model == "mock":
        return MockAnalyst()
    else:
        raise ValueError(f"Unknown AI model: '{model}'. Choose: claude, openai, gemini, openrouter, mock")
