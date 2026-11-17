"""Generator LLM client — vendored from BioNym.

Used ONLY to propose extractions/bindings for JEV to judge — the LLM
enumerates, JEV decides. It never decides anything on its own; every
proposal becomes a JEV question.

Any OpenAI-compatible chat-completions endpoint works: OpenRouter,
OpenAI, Gemini's compatibility endpoint, local vLLM/Ollama. Configure via
env: ``LLM_API_KEY``, ``LLM_BASE_URL`` (default OpenRouter),
``LLM_MODEL``. Disabled when key or model is unset — deterministic
stages run fine without it. ``mock=True`` returns a canned proposal for
dev/tests.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"


class LlmError(RuntimeError):
    pass


class LlmClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        mock: bool = False,
        cache_dir: str | None = None,
        timeout: float = 180.0,
        temperature: float = 0.0,
        max_tokens: int = 16384,
    ) -> None:
        self.base_url = (
            base_url or os.environ.get("LLM_BASE_URL") or DEFAULT_BASE_URL
        ).rstrip("/")
        self.model = model or os.environ.get("LLM_MODEL") or ""
        self.mock = mock
        self.timeout = float(os.environ.get("LLM_TIMEOUT") or timeout)
        # sent explicitly on every call: without it, some OpenAI-compatible
        # deployments apply a low default cap that reasoning models exceed
        # before their JSON finishes (output truncated -> invalid JSON).
        self.max_tokens = int(os.environ.get("LLM_MAX_TOKENS") or max_tokens)
        # min seconds between request starts, enforced across threads:
        # proposal workers may run concurrently, and unfettered bursts
        # hammer shared inference endpoints.
        self.min_interval = float(os.environ.get("LLM_MIN_INTERVAL") or 2.0)
        self._pace_lock = threading.Lock()
        self._last_req = 0.0
        # client-level default for calls that don't specify one
        # (ask/proposal paths). 0.0 = production greedy decoding.
        self.temperature = temperature
        # shared client: keep-alive/connection pooling across calls.
        self._http = httpx.Client()
        self.usage_log: list[dict[str, Any]] = []
        self._api_key = api_key or os.environ.get("LLM_API_KEY") or ""
        self._cache = None
        if cache_dir:
            import diskcache

            self._cache = diskcache.Cache(os.path.join(cache_dir, "llm"))

    @property
    def enabled(self) -> bool:
        """Proposals only run with a configured model — never required."""
        return self.mock or bool(self._api_key and self.model)

    def complete(
        self, system: str, user: str, temperature: float | None = None
    ) -> str:
        """One chat completion -> raw content string (expected JSON)."""
        if self.mock:
            return '{"proposals": [{"claim": "mock proposal", "citations": [1]}], "answer": "mock answer", "cited_nodes": []}'
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
            "temperature": self.temperature
            if temperature is None else temperature,
        }
        if self.max_tokens > 0:
            body["max_tokens"] = self.max_tokens
        key = hashlib.sha256(
            json.dumps(body, sort_keys=True).encode()
        ).hexdigest()
        if self._cache is not None and key in self._cache:
            return self._cache[key]

        # bounded retries on transient failures: shared inference gateways
        # intermittently 5xx or drop connections under load. 4xx errors
        # fail fast -- retrying cannot fix them.
        err: LlmError | None = None
        resp: httpx.Response | None = None
        retries = int(os.environ.get("LLM_RETRIES") or 2)
        delay_s = float(os.environ.get("LLM_RETRY_DELAY_S") or 20)
        for attempt in range(1 + retries):
            with self._pace_lock:
                wait = self.min_interval - (
                    time.monotonic() - self._last_req
                )
                if wait > 0:
                    time.sleep(wait)
                self._last_req = time.monotonic()
            try:
                resp = self._http.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                    timeout=self.timeout,
                )
            except httpx.HTTPError as e:
                err = LlmError(f"LLM request failed: {e}")
            else:
                if resp.status_code == 200:
                    break
                err = LlmError(
                    f"LLM API error {resp.status_code}: {resp.text[:500]}"
                )
                if resp.status_code < 500:
                    raise err
            if attempt < retries:
                # long spacing: a gateway-504'd generation may still be
                # running server-side; an immediate retry doubles the load
                # on a shared endpoint.
                time.sleep(delay_s * (attempt + 1))
        if resp is None or resp.status_code != 200:
            assert err is not None
            raise err
        data = resp.json()
        content = (data.get("choices") or [{}])[0].get("message", {}).get(
            "content", ""
        )
        usage = data.get("usage") or {}
        self.usage_log.append(
            {
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
                # upstream may supply a price; None = unpriced call
                "cost_usd": usage.get("cost_usd")
                or usage.get("cost")
                or usage.get("total_cost"),
            }
        )
        # Don't cache empty responses: a transient null content (reasoning
        # models occasionally return one) would otherwise poison the cache
        # and suppress retries forever.
        if self._cache is not None and content:
            self._cache[key] = content
        return content

    def total_usage(
        self, entries: list[dict[str, Any]] | None = None
    ) -> dict[str, int]:
        """Token totals — over `entries` (a per-audit slice) or the
        whole log when omitted."""
        log = self.usage_log if entries is None else entries
        out = {
            "input_tokens": sum(u["input_tokens"] for u in log),
            "output_tokens": sum(u["output_tokens"] for u in log),
            "calls": len(log),
        }
        priced = [u["cost_usd"] for u in log
                  if u.get("cost_usd") is not None]
        if priced:
            out["cost_usd"] = round(sum(priced), 6)
        return out
