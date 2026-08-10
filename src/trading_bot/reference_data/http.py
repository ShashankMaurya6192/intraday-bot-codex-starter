"""Small verified-HTTPS downloader with explicit retry and timeout bounds."""

from __future__ import annotations

import http.client
import ssl
import time
from collections.abc import Callable
from urllib.parse import urlsplit

from .cache import ReferenceDataError


class ReferenceDataDownloadError(ReferenceDataError):
    """An explicit reference-data refresh could not download its source."""


Sleeper = Callable[[float], None]


def download_https(
    url: str,
    *,
    connect_timeout_seconds: float,
    read_timeout_seconds: float,
    max_attempts: int,
    max_bytes: int,
    sleeper: Sleeper = time.sleep,
) -> bytes:
    """Download bytes using verified TLS, bounded retries, and bounded size."""
    if connect_timeout_seconds <= 0 or read_timeout_seconds <= 0:
        raise ValueError("download timeouts must be positive")
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ReferenceDataDownloadError("reference data source must use HTTPS")

    last_error: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        connection: http.client.HTTPSConnection | None = None
        try:
            connection = http.client.HTTPSConnection(
                parsed.hostname,
                parsed.port or 443,
                timeout=connect_timeout_seconds,
                context=ssl.create_default_context(),
            )
            path = parsed.path or "/"
            if parsed.query:
                path += f"?{parsed.query}"
            connection.request(
                "GET",
                path,
                headers={"User-Agent": "intraday-bot-reference-refresh/0.2"},
            )
            response = connection.getresponse()
            if connection.sock is not None:
                connection.sock.settimeout(read_timeout_seconds)
            if response.status != 200:
                raise ReferenceDataDownloadError(
                    f"reference data server returned HTTP {response.status}"
                )
            payload = response.read(max_bytes + 1)
            if len(payload) > max_bytes:
                raise ReferenceDataDownloadError(
                    "reference data response exceeded the configured size limit"
                )
            return payload
        except (OSError, http.client.HTTPException, ReferenceDataDownloadError) as exc:
            last_error = exc
            if attempt < max_attempts:
                sleeper(min(2 ** (attempt - 1), 4))
        finally:
            if connection is not None:
                connection.close()
    raise ReferenceDataDownloadError(
        f"reference data download failed after {max_attempts} attempts"
    ) from last_error
