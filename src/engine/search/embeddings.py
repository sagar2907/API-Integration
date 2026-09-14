"""Embedding generation and the vector store.

Vectors come from the configured provider's embedding API rather than a local
model, which keeps a half-gigabyte of PyTorch out of the project for a corpus
small enough to search by brute force.

Storage is a single .npz holding an ID array and an L2-normalized float32
matrix. With ~10k endpoints that is roughly 30 MB and a full similarity scan is
a single matrix multiply — a vector database would be pure overhead here.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx
import numpy as np
from sqlalchemy import select
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from engine.config import settings
from engine.db import Api, Endpoint, Provider, session_scope
from engine.search.text import endpoint_document

log = logging.getLogger(__name__)


class EmbeddingError(RuntimeError):
    """Embedding generation failed or is unavailable."""


class RateLimited(RuntimeError):
    """Provider returned 429. Carries the server's requested wait, if any."""

    def __init__(self, retry_after: float | None = None) -> None:
        super().__init__("rate limited")
        self.retry_after = retry_after


def redact(message: str) -> str:
    """Strip the API key from any text before it reaches a log or an exception.

    Defence in depth: keys are sent as headers rather than query parameters, but
    a provider echoing one back, or a library formatting a request, must never
    put it in an error message.
    """
    key = settings.llm_api_key
    if key and len(key) > 8:
        message = message.replace(key, "<REDACTED>")
    return message


def _raise_for_status(response: httpx.Response) -> None:
    """Translate HTTP errors without leaking the request URL or the key."""
    if response.status_code == 429:
        header = response.headers.get("retry-after")
        wait: float | None = None
        if header:
            try:
                wait = float(header)
            except ValueError:
                wait = None
        raise RateLimited(wait)
    if response.status_code >= 400:
        raise EmbeddingError(
            redact(f"embedding request failed: http {response.status_code} {response.text[:300]}")
        )


@dataclass
class VectorStore:
    ids: np.ndarray
    vectors: np.ndarray  # shape (n, d), L2-normalized

    def __len__(self) -> int:
        return int(self.ids.shape[0])

    def search(self, query_vector: np.ndarray, limit: int) -> list[tuple[str, float]]:
        """Cosine similarity via dot product — both sides are unit vectors."""
        if len(self) == 0:
            return []
        scores = self.vectors @ query_vector
        top = np.argpartition(-scores, min(limit, len(scores) - 1))[:limit]
        top = top[np.argsort(-scores[top])]
        return [(str(self.ids[i]), float(scores[i])) for i in top]


def _normalize(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (matrix / norms).astype(np.float32)


# ----------------------------------------------------------------- providers


@retry(
    retry=retry_if_exception_type((httpx.HTTPError, RateLimited)),
    stop=stop_after_attempt(6),
    wait=wait_exponential(multiplier=2, min=2, max=60),
    reraise=True,
)
def _embed_openai(client: httpx.Client, texts: list[str]) -> list[list[float]]:
    response = client.post(
        "https://api.openai.com/v1/embeddings",
        headers={"Authorization": f"Bearer {settings.llm_api_key}"},
        json={"model": settings.embedding_model, "input": texts},
    )
    _raise_for_status(response)
    payload = response.json()
    ordered = sorted(payload["data"], key=lambda item: item["index"])
    return [item["embedding"] for item in ordered]


@retry(
    retry=retry_if_exception_type((httpx.HTTPError, RateLimited)),
    stop=stop_after_attempt(6),
    wait=wait_exponential(multiplier=2, min=2, max=60),
    reraise=True,
)
def _embed_gemini(client: httpx.Client, texts: list[str]) -> list[list[float]]:
    model = settings.embedding_model
    if not model.startswith("models/"):
        model = f"models/{model}"
    response = client.post(
        f"https://generativelanguage.googleapis.com/v1beta/{model}:batchEmbedContents",
        headers={"x-goog-api-key": settings.llm_api_key},
        json={
            "requests": [
                {
                    "model": model,
                    "content": {"parts": [{"text": chunk}]},
                    # Gemini embeddings are natively 3072-d. Truncating to 768
                    # cuts the store from ~130 MB to ~32 MB with little measured
                    # quality loss; the vectors are re-normalized below, which
                    # truncation makes necessary.
                    "outputDimensionality": settings.embedding_dimensions,
                }
                for chunk in texts
            ]
        },
    )
    _raise_for_status(response)
    payload = response.json()
    return [item["values"] for item in payload["embeddings"]]


_local_model = None


def _get_local_model():
    """Load the ONNX embedding model once per process.

    fastembed runs on onnxruntime rather than PyTorch, so this costs a ~130 MB
    model download on first use and nothing after that — no API quota, no
    per-run cost, and identical vectors on every machine, which is what keeps
    the retrieval benchmark reproducible.
    """
    global _local_model
    if _local_model is None:
        from fastembed import TextEmbedding

        log.info("loading local embedding model %s", settings.embedding_model)
        _local_model = TextEmbedding(model_name=settings.embedding_model)
    return _local_model


def _embed_local(_client: httpx.Client | None, texts: list[str]) -> list[np.ndarray]:
    """Return raw numpy rows — converting to Python lists would box every
    component and dominate the runtime once the corpus is large."""
    model = _get_local_model()
    return list(model.embed(texts))


def _batch_size() -> int:
    return settings.embedding_batch_size


def _provider() -> str:
    return settings.embedding_provider.lower()


def _embedder():
    provider = _provider()
    if provider == "local":
        return _embed_local
    if provider == "openai":
        return _embed_openai
    if provider == "gemini":
        return _embed_gemini
    raise EmbeddingError(
        f"unsupported EMBEDDING_PROVIDER {provider!r}; use 'local', 'gemini', or 'openai'"
    )


def embed_texts(
    texts: list[str], *, progress: Callable[[int, int], None] | None = None
) -> np.ndarray:
    """Embed a list of strings, batching and pacing to stay inside quota.

    Free-tier embedding quotas are per-minute, so requests are paced rather than
    fired as fast as the network allows — retrying after a 429 costs far more
    time than spacing requests out in the first place.
    """
    if _provider() != "local" and not settings.llm_api_key:
        raise EmbeddingError(
            "LLM_API_KEY is not set. Create .env from .env.example and add your key, "
            "or set EMBEDDING_PROVIDER=local which needs no key."
        )
    embed = _embedder()
    size = 256 if _provider() == "local" else _batch_size()
    delay = settings.embedding_request_delay_ms / 1000.0
    vectors: list[list[float]] = []

    with httpx.Client(timeout=settings.llm_timeout_seconds) as client:
        for index, start in enumerate(range(0, len(texts), size)):
            chunk = texts[start : start + size]
            if index and delay:
                time.sleep(delay)
            try:
                vectors.extend(embed(client, chunk))
            except RateLimited as err:
                # Every tenacity attempt was exhausted. Surface how far we got
                # so the caller can checkpoint rather than discard the work.
                raise EmbeddingError(
                    f"rate limited after {len(vectors)} of {len(texts)} embeddings. "
                    f"Lower EMBEDDING_BATCH_SIZE or raise EMBEDDING_REQUEST_DELAY_MS "
                    f"(retry-after was {err.retry_after})."
                ) from err
            if progress:
                progress(min(start + size, len(texts)), len(texts))

    return _normalize(np.asarray(vectors, dtype=np.float32))


# -------------------------------------------------------------- build / load


def _embedding_text(document: dict[str, str], method: str, path: str) -> str:
    """One compact string per endpoint. Order matters: the most distinguishing
    signal comes first because embedding models weight early tokens heavily."""
    parts = [
        document["summary"],
        f"{method} {path}",
        document["path_tokens"],
        document["tags"],
        document["provider"],
        document["description"][:400],
    ]
    return " | ".join(part for part in parts if part)


def _checkpoint_path() -> Path:
    return settings.embeddings_path.with_suffix(".partial.npz")


def _load_checkpoint() -> tuple[list[str], list]:
    """Everything already embedded, from either the checkpoint or the finished store.

    The completed store counts as a resume point. Without this, ingesting forty
    new endpoints meant re-embedding the ten thousand that had not changed —
    forty minutes of work to add two.
    """
    for path, label in ((_checkpoint_path(), "checkpoint"), (settings.embeddings_path, "store")):
        if not path.exists():
            continue
        payload = np.load(path, allow_pickle=False)
        ids = [str(x) for x in payload["ids"]]
        vectors = list(payload["vectors"])
        log.info("resuming from existing %s: %d embeddings already done", label, len(ids))
        return ids, vectors
    return [], []


def _save_checkpoint(ids: list[str], vectors: list) -> None:
    if not ids:
        return
    # Uncompressed: compressing a matrix that grows every checkpoint makes the
    # run quadratic. The final store is compressed once, at the end.
    np.savez(
        _checkpoint_path(),
        ids=np.array(ids),
        vectors=np.asarray(vectors, dtype=np.float32),
    )


def _endpoint_texts() -> tuple[list[str], list[str]]:
    """Every endpoint id and the text that represents it, in a stable order."""
    with session_scope() as session:
        rows = session.execute(
            select(
                Endpoint.endpoint_id,
                Endpoint.summary,
                Endpoint.description,
                Endpoint.path,
                Endpoint.method,
                Endpoint.operation_id,
                Endpoint.tags,
                Provider.provider_id,
                Api.name,
            )
            .join(Api, Api.api_id == Endpoint.api_id)
            .join(Provider, Provider.provider_id == Api.provider_id)
            .order_by(Endpoint.endpoint_id)
        ).all()

    ids: list[str] = []
    texts: list[str] = []
    for (
        endpoint_id,
        summary,
        description,
        path,
        method,
        operation_id,
        tags,
        provider_id,
        api_name,
    ) in rows:
        document = endpoint_document(
            summary=summary,
            description=description,
            path=path,
            operation_id=operation_id,
            tags=tags,
            provider=provider_id,
            api_name=api_name,
        )
        ids.append(endpoint_id)
        texts.append(_embedding_text(document, method, path))
    return ids, texts


def build_embeddings(*, resume: bool = True, checkpoint_every: int | None = None) -> int:
    """Embed every endpoint and write the vector store.

    Checkpointed because free-tier quotas are the binding constraint: a run that
    dies 80% of the way through must not throw away 80% of the work. Re-running
    picks up exactly where it stopped.
    """
    if checkpoint_every is None:
        # API providers can die on quota at any moment; local inference cannot,
        # but a ~45 minute run still wants visible progress and a resume point.
        checkpoint_every = 5 if _provider() == "local" else 10
    all_ids, all_texts = _endpoint_texts()
    by_id = dict(zip(all_ids, all_texts, strict=True))

    done_ids, done_vectors = _load_checkpoint() if resume else ([], [])
    done_set = set(done_ids)
    pending = [(eid, by_id[eid]) for eid in all_ids if eid not in done_set]

    if not pending:
        log.info("all %d endpoints already embedded", len(all_ids))
    else:
        log.info(
            "embedding %d endpoints (%d remaining) with %s/%s at %d dims",
            len(all_ids),
            len(pending),
            settings.embedding_provider,
            settings.embedding_model,
            settings.embedding_dimensions,
        )

    embed = _embedder()
    is_local = _provider() == "local"
    size = 256 if is_local else settings.embedding_batch_size
    # Local inference has no quota, so pacing would only waste time.
    delay = 0.0 if is_local else settings.embedding_request_delay_ms / 1000.0

    if pending and not is_local and not settings.llm_api_key:
        raise EmbeddingError("LLM_API_KEY is not set — add it to .env")

    # Free-tier quota is token-based per minute, so the ceiling depends on how
    # long the texts happen to be rather than on a fixed request count. Rather
    # than tune a constant that will be wrong for a different corpus, the batch
    # size adapts: halve on a rate limit, recover slowly after sustained success.
    min_batch, max_batch = 5, size
    current = size
    consecutive_ok = 0
    cursor = 0
    batch_number = 0

    with httpx.Client(timeout=settings.llm_timeout_seconds) as client:
        while cursor < len(pending):
            chunk = pending[cursor : cursor + current]
            if batch_number and delay:
                time.sleep(delay)
            try:
                vectors = embed(client, [chunk_text for _, chunk_text in chunk])
            except RateLimited:
                if current > min_batch:
                    current = max(min_batch, current // 2)
                    consecutive_ok = 0
                    log.warning("rate limited — reducing batch size to %d", current)
                    continue
                _save_checkpoint(done_ids, done_vectors)
                raise EmbeddingError(
                    f"rate limited at the minimum batch size after "
                    f"{len(done_ids)}/{len(all_ids)} embeddings — progress saved. "
                    f"Re-run `engine embed` to resume, or raise "
                    f"EMBEDDING_REQUEST_DELAY_MS."
                ) from None
            except httpx.HTTPError as err:
                _save_checkpoint(done_ids, done_vectors)
                raise EmbeddingError(
                    f"stopped after {len(done_ids)}/{len(all_ids)} embeddings — progress saved. "
                    f"Re-run `engine embed` to resume. Cause: {redact(str(err))[:200]}"
                ) from err

            done_ids.extend(eid for eid, _ in chunk)
            done_vectors.extend(vectors)
            cursor += len(chunk)
            batch_number += 1

            consecutive_ok += 1
            if consecutive_ok >= 10 and current < max_batch:
                current = min(max_batch, current * 2)
                consecutive_ok = 0
                log.info("stable — raising batch size to %d", current)

            if batch_number % checkpoint_every == 0:
                _save_checkpoint(done_ids, done_vectors)
                log.info(
                    "embedded %d/%d (batch=%d, checkpointed)",
                    len(done_ids),
                    len(all_ids),
                    current,
                )

    # Endpoints removed by a later ingestion must not linger in the store.
    live = set(all_ids)
    if any(eid not in live for eid in done_ids):
        keep = [i for i, eid in enumerate(done_ids) if eid in live]
        log.info("dropping %d stale vectors", len(done_ids) - len(keep))
        done_ids = [done_ids[i] for i in keep]
        done_vectors = [done_vectors[i] for i in keep]

    matrix = _normalize(np.asarray(done_vectors, dtype=np.float32))
    settings.embeddings_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(settings.embeddings_path, ids=np.array(done_ids), vectors=matrix)
    _checkpoint_path().unlink(missing_ok=True)
    log.info(
        "wrote %s (%d vectors, dim %d)", settings.embeddings_path, len(done_ids), matrix.shape[1]
    )
    return len(done_ids)


_cache: VectorStore | None = None


def load_vector_store(path: Path | None = None) -> VectorStore:
    """Load the vector store, caching it for the process lifetime."""
    global _cache
    if _cache is not None:
        return _cache
    target = path or settings.embeddings_path
    if not target.exists():
        raise EmbeddingError(
            f"no vector store at {target}. Run `engine embed` first, "
            "or use --mode bm25 which needs no embeddings."
        )
    payload = np.load(target, allow_pickle=False)
    _cache = VectorStore(ids=payload["ids"], vectors=payload["vectors"])
    return _cache


def vector_store_available() -> bool:
    return settings.embeddings_path.exists()


def embed_query(query: str) -> np.ndarray:
    return embed_texts([query])[0]
