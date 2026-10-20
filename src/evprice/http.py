"""Thin HTTP layer with retries. Kept tiny on purpose so it is easy to fake in tests."""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

import requests

from .config import HttpConfig

log = logging.getLogger(__name__)


class SourceError(Exception):
    """An external data source failed or returned something unusable."""


JsonFetcher = Callable[[str, dict[str, Any]], Any]


def make_fetcher(http: HttpConfig, session: requests.Session | None = None, sleep=time.sleep) -> JsonFetcher:
    """Return a function ``fetch(url, params) -> parsed JSON`` that retries transient failures."""
    session = session or requests.Session()
    session.headers.setdefault("User-Agent", "evprice/0.1 (+https://github.com/)")

    def fetch(url: str, params: dict[str, Any]) -> Any:
        last: Exception | None = None
        for attempt in range(1, http.retries + 1):
            try:
                resp = session.get(url, params=params, timeout=http.timeout_seconds)
                if resp.status_code >= 500 or resp.status_code == 429:
                    raise SourceError(f"{url} returned HTTP {resp.status_code}")
                if resp.status_code >= 400:
                    # Client errors will not fix themselves — do not retry.
                    raise SourceError(f"{url} returned HTTP {resp.status_code}: {resp.text[:200]}")
                return resp.json()
            except SourceError as exc:
                if "HTTP 4" in str(exc) and "429" not in str(exc):
                    raise
                last = exc
            except (requests.RequestException, ValueError) as exc:
                last = SourceError(f"{url}: {exc}")
            log.warning("fetch attempt %d/%d failed: %s", attempt, http.retries, last)
            if attempt < http.retries:
                sleep(http.backoff_seconds * attempt)
        assert last is not None
        raise last

    return fetch
