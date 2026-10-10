from __future__ import annotations

import os
from urllib.parse import urlsplit

from .context import build_context
from .model import ModelReply
from .protocol import MmarError, TransientModelError


class ChatCompletionsModel:
    """One request per complete(), bounded transport, no SDK retries or tool access."""

    def __init__(self, *, base_url: str, model: str, api_key: str,
                 max_connections=4, timeout=60, max_output_tokens=1024,
                 token_parameter="max_completion_tokens", json_mode=True,
                 requests_per_second=None, transport=None, max_response_bytes=262144):
        import asyncio
        try:
            import httpx
        except ImportError:
            raise MmarError("missing_httpx_install_api_extra") from None
        url = urlsplit(base_url)
        if url.scheme != "https" and not (url.scheme == "http" and url.hostname in ("localhost", "127.0.0.1", "::1")):
            raise ValueError("API endpoint must use HTTPS or loopback HTTP")
        if not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("Invalid API base URL")
        if not model or not api_key or token_parameter not in ("max_tokens", "max_completion_tokens"):
            raise ValueError("Model, API key and valid token parameter are required")
        if max_connections < 1 or timeout <= 0 or max_output_tokens < 1 or max_response_bytes < 1:
            raise ValueError("Positive transport limits required")
        if requests_per_second is not None and requests_per_second <= 0:
            raise ValueError("Positive request rate required")
        self._endpoint = base_url.rstrip("/") + "/chat/completions"
        self._model, self._key = model, api_key
        self._max_tokens, self._token_parameter = max_output_tokens, token_parameter
        self._json_mode, self._response_bytes = json_mode, max_response_bytes
        self._rate, self._next_request = requests_per_second, 0.0
        self._rate_lock = asyncio.Lock()
        self._client = httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False,
            limits=httpx.Limits(max_connections=max_connections, max_keepalive_connections=max_connections),
            transport=transport)

    @classmethod
    def from_env(cls, **kwargs):
        if not os.environ.get("MMAR_API_KEY") or not os.environ.get("MMAR_MODEL"):
            raise MmarError("missing_MMAR_API_KEY_or_MMAR_MODEL")
        return cls(base_url=os.environ.get("MMAR_API_BASE", "https://api.openai.com/v1"),
                   model=os.environ.get("MMAR_MODEL", ""), api_key=os.environ.get("MMAR_API_KEY", ""),
                   token_parameter=os.environ.get("MMAR_TOKEN_PARAMETER", "max_completion_tokens"),
                   json_mode=os.environ.get("MMAR_JSON_MODE", "1") != "0", **kwargs)

    async def complete(self, snapshot, *, repair=False):
        import asyncio
        import json
        import httpx
        if self._rate:
            async with self._rate_lock:
                loop = asyncio.get_running_loop()
                await asyncio.sleep(max(0, self._next_request - loop.time()))
                self._next_request = loop.time() + 1 / self._rate
        payload = {"model": self._model, "messages": build_context(snapshot, repair),
                   self._token_parameter: self._max_tokens, "stream": False}
        if self._json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            async with self._client.stream("POST", self._endpoint,
                    headers={"Authorization": "Bearer " + self._key}, json=payload) as response:
                if response.status_code in (408, 429) or response.status_code >= 500:
                    raise TransientModelError("provider_temporary_error")
                if response.status_code != 200:
                    raise MmarError("provider_http_error")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > self._response_bytes:
                        raise MmarError("provider_response_too_large")
            data = json.loads(body)
            content = data["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise ValueError()
            usage = data.get("usage")
            return ModelReply(content, usage if isinstance(usage, dict) else None)
        except httpx.TransportError:
            raise TransientModelError("provider_transport_error") from None
        except (ValueError, KeyError, IndexError, TypeError):
            raise MmarError("provider_invalid_response") from None

    async def aclose(self):
        await self._client.aclose()
