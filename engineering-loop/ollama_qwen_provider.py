"""Local, tool-less, fail-closed Ollama/Qwen inference provider."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import time
from typing import Callable, Mapping
from urllib import error, request


PROVIDER_ID = "ollama-qwen"
DEFAULT_ENDPOINT = "http://127.0.0.1:11434"
DEFAULT_MODEL_ID = "qwen3-coder:latest"

_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")


class _NoRedirectHandler(request.HTTPRedirectHandler):
    """Reject every HTTP redirect; V1 is loopback-only."""

    def redirect_request(
        self, req, fp, code, msg, headers, newurl
    ):
        return None


class OllamaQwenProviderError(Exception):
    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category


@dataclass(frozen=True)
class OllamaQwenConfig:
    enabled: bool
    endpoint: str
    model_id: str
    timeout_seconds: float
    max_input_chars: int
    max_output_tokens: int
    max_response_bytes: int

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None
    ) -> "OllamaQwenConfig":
        if env is None:
            import os
            env = os.environ

        enabled_raw = env.get("OLLAMA_QWEN_ENABLED", "false").lower()
        if enabled_raw not in {"true", "false"}:
            raise OllamaQwenProviderError(
                "CONFIGURATION",
                "OLLAMA_QWEN_ENABLED must be true or false",
            )
        enabled = enabled_raw == "true"
        endpoint = env.get("OLLAMA_QWEN_ENDPOINT", DEFAULT_ENDPOINT)
        model_id = env.get("OLLAMA_QWEN_MODEL_ID", DEFAULT_MODEL_ID)

        try:
            timeout_seconds = float(
                env.get("OLLAMA_QWEN_TIMEOUT_SECONDS", "30")
            )
            max_input_chars = int(
                env.get("OLLAMA_QWEN_MAX_INPUT_CHARS", "100000")
            )
            max_output_tokens = int(
                env.get("OLLAMA_QWEN_MAX_OUTPUT_TOKENS", "1024")
            )
            max_response_bytes = int(
                env.get("OLLAMA_QWEN_MAX_RESPONSE_BYTES", "2097152")
            )
        except ValueError as exc:
            raise OllamaQwenProviderError(
                "CONFIGURATION", "numeric configuration is invalid"
            ) from exc

        configured = cls(
            enabled=enabled,
            endpoint=endpoint,
            model_id=model_id,
            timeout_seconds=timeout_seconds,
            max_input_chars=max_input_chars,
            max_output_tokens=max_output_tokens,
            max_response_bytes=max_response_bytes,
        )
        return configured.validated()

    def validated(self) -> "OllamaQwenConfig":
        if self.endpoint != DEFAULT_ENDPOINT:
            raise OllamaQwenProviderError(
                "CONFIGURATION",
                "Ollama endpoint must be exactly http://127.0.0.1:11434",
            )

        if not _MODEL_RE.fullmatch(self.model_id):
            raise OllamaQwenProviderError(
                "CONFIGURATION", "invalid Ollama model id"
            )

        if self.timeout_seconds <= 0 or self.timeout_seconds > 300:
            raise OllamaQwenProviderError(
                "CONFIGURATION", "invalid timeout"
            )

        if self.max_input_chars <= 0:
            raise OllamaQwenProviderError(
                "CONFIGURATION", "invalid input limit"
            )

        if self.max_output_tokens <= 0 or self.max_output_tokens > 32768:
            raise OllamaQwenProviderError(
                "CONFIGURATION", "invalid output token limit"
            )

        if self.max_response_bytes <= 0:
            raise OllamaQwenProviderError(
                "CONFIGURATION", "invalid response size limit"
            )

        return self


@dataclass(frozen=True)
class HTTPResult:
    status_code: int
    body: bytes


@dataclass(frozen=True)
class OllamaQwenResponse:
    provider_id: str
    model_id: str
    response_model_id: str
    content: str
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    latency_ms: int
    attempts: int


def urllib_transport(
    url: str,
    method: str,
    headers: dict[str, str],
    body: bytes | None,
    timeout: float,
    limit: int,
) -> HTTPResult:
    req = request.Request(
        url,
        data=body,
        headers=headers,
        method=method,
    )

    opener = request.build_opener(_NoRedirectHandler())

    try:
        with opener.open(req, timeout=timeout) as response:
            data = response.read(limit + 1)
            if len(data) > limit:
                raise OllamaQwenProviderError(
                    "PROTOCOL", "Ollama response exceeded size limit"
                )
            return HTTPResult(response.status, data)
    except error.HTTPError as exc:
        data = exc.read(limit + 1)
        if len(data) > limit:
            data = data[:limit]
        return HTTPResult(exc.code, data)


class OllamaQwenProvider:
    def __init__(
        self,
        config: OllamaQwenConfig,
        *,
        transport: Callable[..., HTTPResult] = urllib_transport,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.config = config.validated()
        self._transport = transport
        self._clock = clock

    def evaluate(
        self,
        prompt: str,
        *,
        format: str | dict | None = None,
    ) -> OllamaQwenResponse:
        if not self.config.enabled:
            raise OllamaQwenProviderError(
                "CONFIGURATION", "Ollama/Qwen provider is disabled"
            )

        if not isinstance(prompt, str):
            raise OllamaQwenProviderError(
                "CONFIGURATION", "prompt must be text"
            )

        if len(prompt) > self.config.max_input_chars:
            raise OllamaQwenProviderError(
                "INPUT_LIMIT", "prompt exceeds configured input limit"
            )

        if format is not None:
            if isinstance(format, str):
                if format != "json":
                    raise OllamaQwenProviderError(
                        "CONFIGURATION",
                        "format string must be exactly 'json'",
                    )
            elif isinstance(format, dict):
                if not format:
                    raise OllamaQwenProviderError(
                        "CONFIGURATION",
                        "format schema must be a non-empty object",
                    )
            else:
                raise OllamaQwenProviderError(
                    "CONFIGURATION",
                    "format must be 'json', a JSON schema object, or None",
                )

        payload = {
            "model": self.config.model_id,
            "prompt": prompt,
            "stream": False,
            "options": {
                "num_predict": self.config.max_output_tokens,
            },
        }

        if format is not None:
            payload["format"] = format

        body = json.dumps(
            payload, separators=(",", ":")
        ).encode("utf-8")

        start = self._clock()

        try:
            result = self._transport(
                f"{self.config.endpoint}/api/generate",
                "POST",
                {"Content-Type": "application/json"},
                body,
                self.config.timeout_seconds,
                self.config.max_response_bytes,
            )
        except (error.URLError, TimeoutError, OSError) as exc:
            raise OllamaQwenProviderError(
                "TRANSIENT", "local Ollama transport failed"
            ) from exc

        latency_ms = round((self._clock() - start) * 1000)

        if result.status_code != 200:
            category = (
                "TRANSIENT"
                if result.status_code >= 500
                else "PROTOCOL"
            )
            raise OllamaQwenProviderError(
                category,
                f"Ollama returned HTTP {result.status_code}",
            )

        parsed = _json_object(result.body)

        model = parsed.get("model")
        content = parsed.get("response")
        done = parsed.get("done")

        if not isinstance(model, str) or not model:
            raise OllamaQwenProviderError(
                "PROTOCOL", "Ollama response model is invalid"
            )

        if not isinstance(content, str):
            raise OllamaQwenProviderError(
                "PROTOCOL", "Ollama response content is invalid"
            )

        if done is not True:
            raise OllamaQwenProviderError(
                "PROTOCOL", "Ollama response is incomplete"
            )

        prompt_tokens = _optional_token_count(
            parsed.get("prompt_eval_count")
        )
        completion_tokens = _optional_token_count(
            parsed.get("eval_count")
        )

        total_tokens = None
        if prompt_tokens is not None and completion_tokens is not None:
            total_tokens = prompt_tokens + completion_tokens

        return OllamaQwenResponse(
            provider_id=PROVIDER_ID,
            model_id=self.config.model_id,
            response_model_id=model,
            content=content,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            latency_ms=latency_ms,
            attempts=1,
        )


def _json_object(content: bytes) -> dict:
    try:
        parsed = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OllamaQwenProviderError(
            "PROTOCOL", "Ollama returned invalid JSON"
        ) from exc

    if not isinstance(parsed, dict):
        raise OllamaQwenProviderError(
            "PROTOCOL", "Ollama response must be an object"
        )

    return parsed


def _optional_token_count(value: object) -> int | None:
    if value is None:
        return None

    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise OllamaQwenProviderError(
            "PROTOCOL",
            "token usage must be non-negative integers",
        )

    return value
