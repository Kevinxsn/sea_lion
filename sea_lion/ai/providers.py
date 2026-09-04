"""Provider adapters. Both return the same ModelResponse so the router is provider-neutral.

- OpenAICompatProvider: any OpenAI-compatible chat endpoint (local vLLM / llama.cpp). $0.
- AnthropicProvider: official Anthropic SDK with a JSON-schema constrained output.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)


@dataclass
class ModelResponse:
    text: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    model: str
    finish_reason: str = ""
    error: Optional[str] = None


class ProviderError(RuntimeError):
    pass


def check_host_allowed(base_url: str, allowed_hosts: list[str]) -> None:
    host = urlparse(base_url).hostname or ""
    if host not in allowed_hosts:
        raise ProviderError(f"model host '{host}' is not on the allowlist {allowed_hosts}")


class OpenAICompatProvider:
    name = "openai_compat"

    def __init__(self, base_url: str, model: str, timeout_sec: float, allowed_hosts: list[str],
                 api_key: str = "EMPTY", extra_body: Optional[Dict[str, Any]] = None):
        check_host_allowed(base_url, allowed_hosts)
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout_sec
        self.extra_body = dict(extra_body or {})
        self._client = httpx.Client(timeout=httpx.Timeout(timeout_sec, connect=10.0),
                                    headers={"Authorization": f"Bearer {api_key}"})

    def complete_json(self, system: str, user: str, schema: Dict[str, Any], max_tokens: int,
                      temperature: float = 0.0) -> ModelResponse:
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "response_format": {"type": "json_object"},
            **self.extra_body,
        }
        t0 = time.time()
        try:
            r = self._client.post(f"{self.base_url}/chat/completions", json=body)
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise ProviderError(f"{self.name} request failed: {e}") from e
        data = r.json()
        ms = int((time.time() - t0) * 1000)
        try:
            choice = data["choices"][0]
            text = choice["message"].get("content") or ""
            finish = choice.get("finish_reason", "")
        except (KeyError, IndexError) as e:
            raise ProviderError(f"{self.name} malformed response: {e}") from e
        usage = data.get("usage") or {}
        err = None
        if finish == "length" and not text.strip():
            err = f"truncated_at_max_tokens:{max_tokens}"   # reasoning ate the whole budget
        return ModelResponse(text=text, input_tokens=int(usage.get("prompt_tokens", 0)),
                             output_tokens=int(usage.get("completion_tokens", 0)), latency_ms=ms,
                             model=str(data.get("model", self.model)), finish_reason=finish, error=err)

    def healthy(self) -> bool:
        try:
            r = self._client.get(f"{self.base_url}/models", timeout=5.0)
            return r.status_code == 200
        except httpx.HTTPError:
            return False


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, model: str, timeout_sec: float, allowed_hosts: list[str]):
        check_host_allowed("https://api.anthropic.com", allowed_hosts)
        import anthropic
        self._anthropic = anthropic
        self.model = model
        self._client = anthropic.Anthropic(timeout=timeout_sec, max_retries=1)

    def complete_json(self, system: str, user: str, schema: Dict[str, Any], max_tokens: int,
                      temperature: float = 0.0) -> ModelResponse:
        a = self._anthropic
        t0 = time.time()
        try:
            resp = self._client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config={"format": {"type": "json_schema", "schema": _strict(schema)}},
            )
        except (a.RateLimitError, a.APIStatusError, a.APIConnectionError) as e:
            raise ProviderError(f"anthropic request failed: {e}") from e
        ms = int((time.time() - t0) * 1000)
        if resp.stop_reason == "refusal":
            return ModelResponse(text="", input_tokens=resp.usage.input_tokens, output_tokens=resp.usage.output_tokens,
                                 latency_ms=ms, model=resp.model, finish_reason="refusal", error="refusal")
        text = next((b.text for b in resp.content if b.type == "text"), "")
        return ModelResponse(text=text, input_tokens=resp.usage.input_tokens, output_tokens=resp.usage.output_tokens,
                             latency_ms=ms, model=resp.model, finish_reason=str(resp.stop_reason))

    def healthy(self) -> bool:
        return True


def _strict(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Structured outputs want additionalProperties=false on objects."""
    s = dict(schema)
    if s.get("type") == "object":
        s["additionalProperties"] = False
        s["properties"] = {k: _strict(v) if isinstance(v, dict) else v for k, v in s.get("properties", {}).items()}
    if "items" in s and isinstance(s["items"], dict):
        s["items"] = _strict(s["items"])
    if "$defs" in s:
        s["$defs"] = {k: _strict(v) for k, v in s["$defs"].items()}
    return s


def build_provider(tier_cfg, allowed_hosts: list[str], allowed_providers: list[str]):
    if tier_cfg.provider not in allowed_providers:
        raise ProviderError(f"provider '{tier_cfg.provider}' not allowed")
    if tier_cfg.provider == "openai_compat":
        return OpenAICompatProvider(tier_cfg.base_url, tier_cfg.model, tier_cfg.timeout_sec, allowed_hosts,
                                    extra_body=getattr(tier_cfg, "extra_body", None))
    if tier_cfg.provider == "anthropic":
        return AnthropicProvider(tier_cfg.model, tier_cfg.timeout_sec, allowed_hosts)
    raise ProviderError(f"unknown provider {tier_cfg.provider}")
