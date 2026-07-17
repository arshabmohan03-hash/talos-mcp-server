"""Async multi-provider AI client (Cerebras → Groq) with key/model failover.

Both providers expose an OpenAI-style streaming chat API with function calling,
so the agent loop consumes their chunks identically. Cerebras is tried first
(the default), then Groq — so a rate-limit or outage on one fails over to the
other automatically. Keys live only in .env, never in source.
"""
from __future__ import annotations

from app.config import get_settings

_CLIENTS: dict[tuple[str, str], object] = {}


def _client(provider: str, api_key: str):
    key = (provider, api_key)
    if key not in _CLIENTS:
        if provider == "groq":
            from groq import AsyncGroq
            _CLIENTS[key] = AsyncGroq(api_key=api_key)
        else:
            from cerebras.cloud.sdk import AsyncCerebras
            _CLIENTS[key] = AsyncCerebras(api_key=api_key)
    return _CLIENTS[key]


def _attempts() -> list[tuple[str, str, str]]:
    """(provider, api_key, model) triples to try in order — Cerebras then Groq."""
    s = get_settings()
    out: list[tuple[str, str, str]] = []
    for key in s.api_keys():                                  # Cerebras (primary)
        for model in [m for m in (s.default_model, s.fallback_model) if m]:
            out.append(("cerebras", key, model))
    for key in s.groq_keys():                                 # Groq (failover)
        for model in [m for m in (s.groq_model, s.groq_fallback_model) if m]:
            out.append(("groq", key, model))
    return out


def _kwargs(model: str, messages: list[dict], tools: list[dict] | None,
            max_tokens: int, stream: bool) -> dict:
    kw = dict(model=model, messages=messages, max_completion_tokens=max_tokens)
    if stream:
        kw["stream"] = True
    if tools:
        kw["tools"] = tools
        kw["tool_choice"] = "auto"
    return kw


async def chat_completion(messages: list[dict], tools: list[dict] | None = None,
                          max_tokens: int = 4000):
    """Call the AI with automatic failover across providers, keys, then models.

    Returns (message, model_used). Raises the last error if every attempt fails.
    """
    last_err: Exception | None = None
    for provider, api_key, model in _attempts():
        try:
            resp = await _client(provider, api_key).chat.completions.create(
                **_kwargs(model, messages, tools, max_tokens, False))
            return resp.choices[0].message, model
        except Exception as e:  # noqa: BLE001
            last_err = e
            continue
    raise RuntimeError(f"All AI attempts failed. Last error: {last_err}")


async def stream_completion(messages: list[dict], tools: list[dict] | None = None,
                            max_tokens: int = 4000):
    """Stream a completion, yielding deltas as they arrive.

    Yields:
        ("model", model_name)   once, before the first chunk
        ("chunk", sdk_chunk)    for every streamed delta (OpenAI-style for both providers)

    Failover across (provider, key, model) happens only *before* the first chunk;
    once tokens flow we commit, so a mid-stream error propagates rather than
    restarting with garbled output.
    """
    last_err: Exception | None = None
    for provider, api_key, model in _attempts():
        yielded = False
        try:
            stream = await _client(provider, api_key).chat.completions.create(
                **_kwargs(model, messages, tools, max_tokens, True))
            async for chunk in stream:
                if not yielded:
                    yield ("model", model)
                    yielded = True
                yield ("chunk", chunk)
            return
        except Exception as e:  # noqa: BLE001
            last_err = e
            if yielded:
                raise
            continue
    raise RuntimeError(f"All AI streaming attempts failed. Last error: {last_err}")
