"""Language-model access.

One narrow interface — "here is a prompt, give me back JSON" — with a Gemini
and an OpenAI implementation behind it, plus a stub for tests. Everything above
this module is provider-agnostic.

Structured output is requested through the provider's JSON mode, but the real
guarantee comes afterwards: the response is parsed into a Pydantic model with
`extra="forbid"`. Provider JSON modes vary in dialect and strictness, so the
model is treated as untrusted regardless of what it promises.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from engine.config import settings

log = logging.getLogger(__name__)

# Tokens a thinking model may spend deliberating before answering. Zero would
# disable thinking entirely; a small allowance keeps quality on the harder
# planning prompts without letting reasoning crowd out the response.
THINKING_BUDGET = 512


class LLMError(RuntimeError):
    """The model could not be reached, or returned something unusable."""


@dataclass
class Usage:
    """Token accounting, so the cost of a plan is measurable rather than guessed."""

    prompt_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0

    def add(self, other: Usage) -> None:
        self.prompt_tokens += other.prompt_tokens
        self.output_tokens += other.output_tokens
        self.calls += other.calls

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.output_tokens


@dataclass
class Completion:
    data: dict[str, Any]
    raw: str
    usage: Usage = field(default_factory=Usage)


class LLMClient(Protocol):
    """What the rest of the engine needs from a language model."""

    def complete_json(self, *, system: str, user: str, max_tokens: int = 4096) -> Completion: ...


def _extract_json(text: str) -> dict[str, Any]:
    """Parse a JSON object out of a model response.

    Models wrap JSON in prose or fences even when asked not to, so the first
    balanced object is extracted rather than trusting the whole string.
    """
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1] if text.count("```") >= 2 else text
        text = text.removeprefix("json").strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise LLMError(f"model returned no JSON object: {text[:200]!r}") from None
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError as err:
            raise LLMError(f"model returned malformed JSON: {err}") from err
    if not isinstance(parsed, dict):
        raise LLMError(f"expected a JSON object, got {type(parsed).__name__}")
    return parsed


def _redact(message: str) -> str:
    key = settings.llm_api_key
    return message.replace(key, "<REDACTED>") if key and len(key) > 8 else message


class GeminiClient:
    """Google Generative Language API.

    The key travels as a header, never as a query parameter: httpx puts the full
    URL into exception messages, so a key in the query string leaks into every
    timeout and rate-limit traceback.
    """

    def __init__(self, model: str | None = None, api_key: str | None = None) -> None:
        self.model = model or settings.llm_model
        self.api_key = api_key or settings.llm_api_key
        if not self.api_key:
            raise LLMError("LLM_API_KEY is not set — add it to .env")

    def complete_json(self, *, system: str, user: str, max_tokens: int = 4096) -> Completion:
        model = self.model if self.model.startswith("models/") else f"models/{self.model}"
        payload = {
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "systemInstruction": {"parts": [{"text": system}]},
            "generationConfig": {
                "responseMimeType": "application/json",
                # Planning is a structured extraction task, not a creative one;
                # determinism also makes the repair loop reproducible.
                "temperature": 0,
                "maxOutputTokens": max_tokens,
                # Reasoning tokens are billed against maxOutputTokens on
                # thinking models. Left unbounded on a long planning prompt they
                # consume the entire budget and the response comes back as
                # truncated reasoning instead of an answer. Extracting a
                # structured plan from an explicit candidate list needs little
                # deliberation, so the budget is capped rather than spent.
                "thinkingConfig": {"thinkingBudget": THINKING_BUDGET},
            },
        }
        try:
            with httpx.Client(timeout=settings.llm_timeout_seconds) as client:
                response = client.post(
                    f"https://generativelanguage.googleapis.com/v1beta/{model}:generateContent",
                    headers={"x-goog-api-key": self.api_key},
                    json=payload,
                )
        except httpx.HTTPError as err:
            raise LLMError(_redact(f"request failed: {err}")) from err

        if response.status_code >= 400:
            raise LLMError(_redact(f"http {response.status_code}: {response.text[:300]}"))

        body = response.json()
        candidates = body.get("candidates") or []
        if not candidates:
            raise LLMError(f"model returned no candidates: {str(body)[:200]}")
        finish_reason = candidates[0].get("finishReason")
        parts = candidates[0].get("content", {}).get("parts") or []
        # Thinking models may return their reasoning as separate parts; joining
        # those into the answer produces unparseable output.
        text = "".join(part.get("text", "") for part in parts if not part.get("thought"))
        if not text.strip():
            raise LLMError(
                f"model returned no answer (finishReason={finish_reason}). "
                "If this is MAX_TOKENS, raise max_tokens or lower the thinking budget."
            )
        meta = body.get("usageMetadata") or {}
        return Completion(
            data=_extract_json(text),
            raw=text,
            usage=Usage(
                prompt_tokens=int(meta.get("promptTokenCount", 0)),
                output_tokens=int(meta.get("candidatesTokenCount", 0)),
                calls=1,
            ),
        )


class OpenAIClient:
    def __init__(self, model: str | None = None, api_key: str | None = None) -> None:
        self.model = model or settings.llm_model
        self.api_key = api_key or settings.llm_api_key
        if not self.api_key:
            raise LLMError("LLM_API_KEY is not set — add it to .env")

    def complete_json(self, *, system: str, user: str, max_tokens: int = 4096) -> Completion:
        try:
            with httpx.Client(timeout=settings.llm_timeout_seconds) as client:
                response = client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={
                        "model": self.model,
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": user},
                        ],
                        "response_format": {"type": "json_object"},
                        "temperature": 0,
                        "max_tokens": max_tokens,
                    },
                )
        except httpx.HTTPError as err:
            raise LLMError(_redact(f"request failed: {err}")) from err

        if response.status_code >= 400:
            raise LLMError(_redact(f"http {response.status_code}: {response.text[:300]}"))

        body = response.json()
        text = body["choices"][0]["message"]["content"]
        meta = body.get("usage") or {}
        return Completion(
            data=_extract_json(text),
            raw=text,
            usage=Usage(
                prompt_tokens=int(meta.get("prompt_tokens", 0)),
                output_tokens=int(meta.get("completion_tokens", 0)),
                calls=1,
            ),
        )


class StubClient:
    """Replays canned responses. Used by every test that touches planning.

    Real-model behaviour belongs in the benchmark suite, not the test suite: a
    test that calls a live API is slow, costs money, and fails for reasons that
    have nothing to do with the code under test.
    """

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = list(responses)
        self.prompts: list[tuple[str, str]] = []

    def complete_json(self, *, system: str, user: str, max_tokens: int = 4096) -> Completion:
        self.prompts.append((system, user))
        if not self._responses:
            raise LLMError("stub client ran out of scripted responses")
        data = self._responses.pop(0)
        if isinstance(data, Exception):
            raise data
        return Completion(data=data, raw=json.dumps(data), usage=Usage(calls=1))


def get_client() -> LLMClient:
    provider = settings.llm_provider.lower()
    if provider == "gemini":
        return GeminiClient()
    if provider == "openai":
        return OpenAIClient()
    raise LLMError(f"unsupported LLM_PROVIDER {provider!r}; use 'gemini' or 'openai'")
