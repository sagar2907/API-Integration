"""Download OpenAPI specifications from the APIs.guru directory.

APIs.guru publishes a few thousand real-world OpenAPI/Swagger documents, which is
what makes a realistic corpus reachable in an afternoon instead of a month. Specs
are cached on disk so re-running ingestion costs nothing.
"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from engine.config import settings

log = logging.getLogger(__name__)

# A handful of providers publish 20 MB+ documents. They are slow to download, slow
# to parse, and would dominate the corpus; the endpoint cap would truncate them
# anyway. GitHub's 5.8 MB spec is the largest we deliberately keep.
MAX_SPEC_BYTES = 8_000_000

# Version markers that appear inside API titles.
_VERSION_NOISE = re.compile(r"\b(v\d+(\.\d+)*|\d+\.\d+(\.\d+)*|20\d{2}(-\d+)*)\b")

# Providers worth having in the corpus regardless of directory ordering: they are
# recognisable, well-documented, and the demo workflows target them.
PRIORITY_PROVIDERS = (
    "github.com",
    "slack.com",
    "stripe.com",
    "sendgrid.com",
    "twilio.com",
    "notion.com",
    "spotify.com",
    "zoom.us",
    "shopify.com",
    "asana.com",
    "trello.com",
    "dropbox.com",
    "box.com",
    "hubspot.com",
    "mailchimp.com",
    "googleapis.com",
    "microsoft.com",
    "atlassian.com",
    "twitter.com",
    "discord.com",
)


@dataclass(frozen=True)
class SpecRef:
    """A specific version of a specific API in the directory."""

    key: str  # e.g. "github.com" or "googleapis.com:calendar"
    version: str
    url: str
    title: str
    provider_name: str

    @property
    def cache_name(self) -> str:
        safe = self.key.replace(":", "__").replace("/", "_")
        return f"{safe}@{self.version.replace('/', '_')}.json"


def load_directory(client: httpx.Client) -> dict[str, Any]:
    log.info("fetching APIs.guru directory")
    response = client.get(settings.apis_guru_list_url)
    response.raise_for_status()
    directory: dict[str, Any] = response.json()
    log.info("directory contains %d entries", len(directory))
    return directory


def _canonicality(ref: SpecRef) -> tuple[int, int, str]:
    """Rank variants of one provider so the flagship API wins.

    APIs.guru carries many near-duplicate documents per provider — GitHub alone
    publishes the public API plus every GitHub Enterprise Server release, each a
    6 MB near-copy. Without this, a corpus of 12 specs is 12 copies of GitHub.
    """
    key = ref.key
    suffix = key.split(":", 1)[1] if ":" in key else ""
    # Enterprise/versioned editions are duplicates of the flagship spec.
    is_variant = 1 if any(marker in suffix for marker in ("ghes", "ghec", "enterprise")) else 0
    return (is_variant, len(key), key)


def _dedup_key(ref: SpecRef) -> tuple[str, str]:
    """Group specs that are genuinely the same API.

    Deduplicating by provider alone was wrong, and wrong in a way that quietly
    destroyed coverage. Two different things share a provider:

      github.com, github.com:ghes-3.1   -> the same API, 20 near-identical copies
      googleapis.com:gmail, :calendar   -> 281 *different products*

    Both look like "provider plus a suffix", so a per-provider cap keeps one
    GitHub (right) and one Google service out of 281 (catastrophic — it is how
    Gmail came to be missing while a machine-learning API stood in for it).

    Grouping by provider *and API title* separates the two cases: the GitHub
    editions all publish the title "GitHub v3 REST API", while Gmail and
    Calendar publish different titles.
    """
    title = re.sub(r"[^a-z0-9]+", " ", ref.title.lower())
    # Version markers must not make one edition look like a different product:
    # "GitHub v3 REST API" and "GitHub REST API" name the same thing. The word
    # boundaries matter — without them this would also strip the digits out of
    # names like "api2cart" or "s3".
    title = _VERSION_NOISE.sub(" ", title)
    return (ref.provider_name, " ".join(title.split()))


def select_specs(directory: dict[str, Any], limit: int, *, per_group: int = 1) -> list[SpecRef]:
    """Pick `limit` specs, priority providers first, then the rest alphabetically.

    Only the 'preferred' version of each API is taken, and at most `per_group`
    documents per (provider, API) pair — indexing six editions of one API
    inflates the endpoint count without making retrieval any harder, while
    distinct products from the same vendor are all kept.
    """
    refs: list[SpecRef] = []
    for key, entry in directory.items():
        versions = entry.get("versions") or {}
        preferred = entry.get("preferred") or next(iter(versions), None)
        version_info = versions.get(preferred) if preferred else None
        if not version_info:
            continue
        url = version_info.get("swaggerUrl") or version_info.get("swaggerYamlUrl")
        if not url:
            continue
        info = version_info.get("info") or {}
        refs.append(
            SpecRef(
                key=key,
                version=str(preferred),
                url=url,
                title=str(info.get("title") or key),
                provider_name=str(info.get("x-providerName") or key.split(":")[0]),
            )
        )

    # Keep only the most canonical document per distinct API.
    groups: dict[tuple[str, str], list[SpecRef]] = {}
    for ref in refs:
        groups.setdefault(_dedup_key(ref), []).append(ref)
    deduped = [
        ref for group in groups.values() for ref in sorted(group, key=_canonicality)[:per_group]
    ]

    def sort_key(ref: SpecRef) -> tuple[int, str]:
        for index, provider in enumerate(PRIORITY_PROVIDERS):
            if ref.provider_name == provider or ref.key.startswith(provider):
                return (index, ref.key)
        return (len(PRIORITY_PROVIDERS), ref.key)

    deduped.sort(key=sort_key)
    log.info(
        "%d distinct APIs available across %d providers, %d after dedup",
        len(groups),
        len({r.provider_name for r in refs}),
        len(deduped),
    )
    return deduped[:limit]


def fetch_spec(
    client: httpx.Client,
    ref: SpecRef,
    *,
    force: bool = False,
    max_bytes: int = MAX_SPEC_BYTES,
) -> Path | None:
    """Download one spec into the cache. Returns None if it could not be fetched.

    Streams so an oversized document can be abandoned mid-download rather than
    spending minutes on a spec that would dominate the corpus anyway.
    """
    target = settings.specs_dir / ref.cache_name
    if target.exists() and not force:
        return target
    try:
        with client.stream("GET", ref.url) as response:
            response.raise_for_status()
            declared = response.headers.get("content-length")
            if declared and int(declared) > max_bytes:
                log.info("skipping %s: %s bytes exceeds cap", ref.key, declared)
                return None
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    log.info("skipping %s: body exceeded %d bytes", ref.key, max_bytes)
                    return None
                chunks.append(chunk)
        payload = json.loads(b"".join(chunks))
    except (httpx.HTTPError, json.JSONDecodeError, ValueError) as err:
        log.warning("failed to fetch %s: %s", ref.key, err)
        return None
    target.write_text(json.dumps(payload), encoding="utf-8")
    return target


def fetch_all(
    limit: int | None = None,
    *,
    force: bool = False,
    per_group: int = 1,
    workers: int = 8,
    include: tuple[str, ...] = (),
) -> list[tuple[SpecRef, Path]]:
    """Download the selected specs concurrently.

    Downloads are I/O bound and independent, and a handful of very large documents
    would otherwise serialise the whole run behind them.
    """
    limit = limit or settings.ingest_spec_limit
    results: list[tuple[SpecRef, Path]] = []
    completed = 0

    with httpx.Client(
        timeout=httpx.Timeout(60.0, connect=15.0),
        follow_redirects=True,
        limits=httpx.Limits(max_connections=workers * 2),
    ) as client:
        directory = load_directory(client)
        refs = select_specs(directory, limit, per_group=per_group)
        # Explicitly requested keys are added regardless of ranking, so a
        # specific API can be pulled in without widening the whole corpus.
        if include:
            chosen = {r.key for r in refs}
            for extra in select_specs(directory, len(directory), per_group=per_group):
                if extra.key in chosen:
                    continue
                if any(term.lower() in extra.key.lower() for term in include):
                    refs.append(extra)
                    chosen.add(extra.key)
        log.info("selected %d specs, downloading with %d workers", len(refs), workers)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(fetch_spec, client, ref, force=force): ref for ref in refs}
            for future in as_completed(futures):
                ref = futures[future]
                completed += 1
                try:
                    path = future.result()
                except Exception as err:  # a single bad download never fails the run
                    log.warning("error fetching %s: %s", ref.key, err)
                    path = None
                if path is not None:
                    results.append((ref, path))
                if completed % 20 == 0 or completed == len(refs):
                    log.info("fetched %d/%d (%d cached)", completed, len(refs), len(results))

    log.info("cached %d specs in %s", len(results), settings.specs_dir)
    return results
