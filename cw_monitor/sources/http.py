"""Bounded HTTPS transport and Retry-After handling."""

from __future__ import annotations

import json
import math
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..config import MAX_RESPONSE_BYTES, USER_AGENT, UTC
from ..models import SourceError
from ..values import clean


def get_text(url: str, timeout: float) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json,text/html"})
    try:
        with urlopen(request, timeout=timeout) as response:
            data = response.read(MAX_RESPONSE_BYTES + 1)
            if len(data) > MAX_RESPONSE_BYTES:
                raise SourceError(f"Response exceeds {MAX_RESPONSE_BYTES} bytes: {url.split('?')[0]}")
            return data.decode("utf-8")
    except HTTPError as exc:
        delay = 0.0
        retry = exc.headers.get("Retry-After", "")
        if retry:
            try:
                delay = max(0, float(retry))
            except ValueError:
                from email.utils import parsedate_to_datetime
                try:
                    when = parsedate_to_datetime(retry)
                    if when.tzinfo is None:
                        when = when.replace(tzinfo=UTC)
                    delay = max(0, when.timestamp() - time.time())
                except (ValueError, TypeError, OverflowError):
                    pass
        if not math.isfinite(delay):
            delay = 0
        if exc.fp is not None:
            exc.close()
        raise SourceError(f"HTTP {exc.code}: {url.split('?')[0]}", delay) from exc
    except (URLError, TimeoutError, OSError, UnicodeError) as exc:
        raise SourceError(f"Connection to {url.split('?')[0]}: {clean(exc)}") from exc


def get_json(url: str, timeout: float) -> object:
    try:
        return json.loads(get_text(url, timeout))
    except ValueError as exc:
        raise SourceError(f"Invalid JSON: {url.split('?')[0]}") from exc
