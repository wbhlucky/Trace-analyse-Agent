"""OpenAI-compatible HTTP embedding provider.

This is the "real model" side of P3: point it at any ``/embeddings`` endpoint
(OpenAI, DeepSeek, vLLM, Ollama, a company gateway) and hybrid recall uses real
semantic vectors instead of the offline recipe. It is written against
``urllib.request`` on purpose - the runtime dependency list stays at five
packages, and an embedding call is one POST whose only interesting property is
that it must never take the analysis run down with it.

Every failure mode returns ``None`` vectors rather than raising: unreachable
host, 401, rate limit, malformed JSON, a response shorter than the request.
Recall then behaves exactly as it does with no provider configured, which is the
degradation the plan requires. The last error is kept on :attr:`last_error` so
``memory stats`` can tell an operator "semantic recall is off because your key
expired" instead of leaving them to guess.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Sequence

from trace_agent.memory.embedding.ports import Vector

DEFAULT_TIMEOUT_S = 10.0
DEFAULT_BATCH_SIZE = 16
# Bounds one request body. Embedding a whole memory is unnecessary - the topic,
# content and keywords are what recall compares - and an unbounded payload turns
# a long record into a 413 and a silently unembedded memory.
MAX_INPUT_CHARS = 8_000


class HttpEmbeddingProvider:
    """``POST {base_url}/embeddings`` with OpenAI request/response shape."""

    name = "http"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str = "",
        model: str = "text-embedding-3-small",
        dim: int = 0,
        timeout: float = DEFAULT_TIMEOUT_S,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        # The configured model name is the stored identity, not whatever the
        # server echoes: a gateway that rewrites the name would otherwise
        # invalidate the whole corpus on every request.
        self.model = model
        # 0 means "learn it from the first response". Vectors of differing
        # length compare as 0.0, so a later change degrades instead of poisoning
        # the ranking, and a rebuild fixes it.
        self.dim = int(dim) if dim and dim > 0 else 0
        self.timeout = timeout if timeout and timeout > 0 else DEFAULT_TIMEOUT_S
        self.batch_size = max(1, int(batch_size or DEFAULT_BATCH_SIZE))
        self.last_error: str | None = None

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/embeddings"

    def embed(self, text: str) -> Vector | None:
        vectors = self.embed_batch([text])
        return vectors[0] if vectors else None

    def embed_batch(self, texts: Sequence[str]) -> list[Vector | None]:
        """Vectors parallel to ``texts``; ``None`` per entry that failed."""
        if not self.base_url:
            self.last_error = "no base_url configured"
            return [None] * len(texts)
        out: list[Vector | None] = []
        for start in range(0, len(texts), self.batch_size):
            out.extend(self._post(list(texts[start : start + self.batch_size])))
        return out

    def _post(self, texts: Sequence[str]) -> list[Vector | None]:
        payload = json.dumps(
            {
                "model": self.model,
                "input": [text[:MAX_INPUT_CHARS] for text in texts],
            }
        ).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(  # noqa: S310 - operator-configured URL
            self.endpoint, data=payload, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            self.last_error = f"HTTP {exc.code} from {self.endpoint}"
            return [None] * len(texts)
        except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return [None] * len(texts)
        return self._parse(body, expected=len(texts))

    def _parse(self, body: object, *, expected: int) -> list[Vector | None]:
        """Turn an OpenAI-shaped response into positional vectors.

        Sorted by the server's ``index`` rather than trusted in arrival order:
        batch endpoints are documented to return items out of order, and a
        misaligned batch would attach each memory's vector to its neighbour -
        the kind of corruption that shows up as "recall got weird" with nothing
        in the logs.
        """
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list):
            self.last_error = "response has no data array"
            return [None] * expected
        slots: list[Vector | None] = [None] * expected
        for position, item in enumerate(data):
            if not isinstance(item, dict):
                continue
            raw = item.get("embedding")
            if not isinstance(raw, list):
                continue
            try:
                vector = [float(value) for value in raw]
            except (TypeError, ValueError):
                self.last_error = "embedding contains a non-numeric value"
                continue
            if not vector:
                continue
            index = item.get("index")
            slot = index if isinstance(index, int) and 0 <= index < expected else position
            slots[slot] = vector
            if self.dim == 0:
                self.dim = len(vector)
        if all(slot is None for slot in slots):
            self.last_error = self.last_error or "response contained no usable embeddings"
        else:
            self.last_error = None
        return slots

    def __repr__(self) -> str:  # pragma: no cover - diagnostics
        return (
            f"HttpEmbeddingProvider(base_url={self.base_url!r}, "
            f"model={self.model!r}, dim={self.dim})"
        )


__all__ = ["HttpEmbeddingProvider"]
