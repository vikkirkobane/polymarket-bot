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

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-5"):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.model = model

    async def _call_model(self, prompt: str) -> str:
        message = await self.client.messages.create(
            model=self.model,
            max_tokens=1500,
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
            max_tokens=1500,
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

    # Verified against the account's key (models.list) — flash tier rotates fast,
    # so fall through when a generation is overloaded (503) or quota-capped (429).
    _FALLBACK_MODELS = [
        "gemini-3.8-flash",
        "gemini-3.6-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
    ]

    def __init__(self, api_key: str, model: str = "gemini-3.8-flash"):
        from google import genai
        self.client = genai.Client(api_key=api_key, http_options={'api_version': 'v1'})
        self.model_id = model

    async def _call_model(self, prompt: str) -> str:
        # Deduplicate preferred model first
        seen: set = set()
        ordered = [m for m in [self.model_id] + self._FALLBACK_MODELS if not (m in seen or seen.add(m))]
        last_err: Exception | None = None
        for mid in ordered:
            try:
                # Wrap synchronous SDK call in a thread to avoid blocking the event loop
                response = await asyncio.to_thread(
                    self.client.models.generate_content,
                    model=mid,
                    contents=prompt,
                )
                if response and response.text:
                    if mid != self.model_id:
                        logger.info(f"Gemini: switched to {mid}")
                        self.model_id = mid
                    return response.text
                last_err = RuntimeError(f"Gemini {mid} returned empty content")
            except Exception as exc:
                last_err = exc
                err_str = str(exc)
                if any(tag in err_str for tag in ("503", "429", "NOT_FOUND", "no longer available")):
                    logger.warning(f"Gemini {mid} unavailable, trying next: {err_str[:80]}")
                    continue
                raise
        raise last_err or RuntimeError("All Gemini fallback models exhausted")


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

    def __init__(self, api_key: str, model: str = "google/gemma-4-31b-it:free"):
        # OpenRouter is OpenAI-compatible
        self.client = openai_lib.AsyncOpenAI(
            api_key=api_key,
            base_url="https://openrouter.ai/api/v1",
            timeout=90.0,
        )
        self.model = model
        self._extra_headers = {
            "HTTP-Referer": "https://github.com/polymarket-bot", # For OpenRouter rankings
            "X-Title": "Polymarket Bot",
        }
        # Free models cap total context+completion tokens; start modest and adapt
        # to whatever the account can actually afford (see 402 handling below).
        self.max_tokens = 1200

    @staticmethod
    def _affordable_tokens(err_text: str) -> int | None:
        """Parse OpenRouter's 'can only afford N tokens' message from a 402 error."""
        import re
        m = re.search(r"can only afford (\d+) tokens", err_text)
        return int(m.group(1)) if m else None

    async def _call_model(self, prompt: str) -> str:
        # Ordered fallback list — tries each model in sequence if the previous is unavailable.
        # All slugs verified live against openrouter.ai/api/v1/models (2026-10-15).
        fallbacks = [
            self.model,
            "google/gemma-4-31b-it:free",
            "nvidia/nemotron-3.5-lightning:free",
            "nvidia/nemotron-3-super-120b-a12b:free",
            "nvidia/nemotron-3-ultra-550b-a55b:free",
            "poolside/laguna-s-2.1:free",
        ]
        # Deduplicate while preserving order
        seen: set = set()
        ordered = [m for m in fallbacks if not (m in seen or seen.add(m))]

        last_err = None
        for model_name in ordered:
            for attempt in range(2):  # one chance to adapt to the account's token budget
                try:
                    response = await self.client.chat.completions.create(
                        model=model_name,
                        messages=[{"role": "user", "content": prompt}],
                        extra_headers=self._extra_headers,
                        max_tokens=self.max_tokens,
                    )
                    content = response.choices[0].message.content if response.choices else None
                    if content:
                        if model_name != self.model:
                            logger.info(f"OpenRouter: switched to {model_name}")
                            self.model = model_name
                        return content
                    # Reasoning models can spend the whole completion budget on thinking
                    # and return empty content — disable reasoning and retry once.
                    if not getattr(self, "_no_reason_retry", False):
                        self._no_reason_retry = True
                        logger.info(f"OpenRouter {model_name}: empty content — retrying with reasoning disabled")
                        response = await self.client.chat.completions.create(
                            model=model_name,
                            messages=[{"role": "user", "content": prompt}],
                            extra_headers=self._extra_headers,
                            max_tokens=self.max_tokens,
                            extra_body={"reasoning": {"effort": "none"}},
                        )
                        content = response.choices[0].message.content if response.choices else None
                        if content:
                            self._no_reason_retry = False
                            return content
                    logger.warning(f"OpenRouter {model_name} returned empty content, trying next")
                    break  # empty content isn't a budget problem — move to next model
                except Exception as e:
                    last_err = e
                    err_str = str(e)
                    if "402" in err_str:
                        affordable = self._affordable_tokens(err_str)
                        if affordable and affordable - 64 < self.max_tokens:
                            # Shrink the completion budget to fit the account and retry
                            self.max_tokens = max(256, affordable - 64)
                            logger.warning(
                                f"OpenRouter {model_name}: 402 budget error — retrying with "
                                f"max_tokens={self.max_tokens}"
                            )
                            continue
                        logger.warning(f"OpenRouter {model_name} out of credits, trying next")
                        break
                    if any(code in err_str for code in ("404", "400", "503", "502", "529")):
                        logger.warning(f"OpenRouter {model_name} unavailable ({err_str[:60]}), trying next")
                        break  # model not found — don't retry same model
                    if "free-models-per-day" in err_str:
                        # Account-wide cap shared by ALL :free models — retrying others is futile
                        logger.error(
                            "OpenRouter daily free-tier quota exhausted. Wait for the daily reset "
                            "or add ~$5 credits to your OpenRouter account to unlock paid models."
                        )
                        raise last_err
                    if "429" in err_str:
                        await asyncio.sleep(2)
                        continue  # rate-limited — one patient retry
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
        return ClaudeAnalyst(api_key=settings.anthropic_api_key, model=settings.anthropic_model)
    elif model == "openai":
        if not settings.openai_api_key:
            raise ValueError("OPENAI_API_KEY is not set")
        return OpenAIAnalyst(api_key=settings.openai_api_key, model=settings.openai_model)
    elif model == "gemini":
        if not settings.google_api_key:
            raise ValueError("GOOGLE_API_KEY is not set")
        return GeminiAnalyst(api_key=settings.google_api_key, model=settings.gemini_model)
    elif model == "openrouter":
        if not settings.openrouter_api_key:
            raise ValueError("OPENROUTER_API_KEY is not set")
        return OpenRouterAnalyst(api_key=settings.openrouter_api_key, model=settings.openrouter_model)
    elif model == "mock":
        return MockAnalyst()
    else:
        raise ValueError(f"Unknown AI model: '{model}'. Choose: claude, openai, gemini, openrouter, mock")
