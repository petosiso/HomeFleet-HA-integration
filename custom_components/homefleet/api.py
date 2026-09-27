"""BlackLabs Watchdog HTTP transport."""

import asyncio
import json
import logging

from aiohttp import ClientError, ClientSSLError, ClientTimeout
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import MAX_REPORT_BYTES

_LOGGER = logging.getLogger(__name__)


class InvalidKeyError(Exception):
    """The backend rejected the integration key."""


class BackendUnavailableError(Exception):
    """A safe transport failure category, without URL, headers or response data."""

    def __init__(self, reason: str = "connection_error"):
        self.reason = reason
        super().__init__(reason)


class BackendResponseError(BackendUnavailableError):
    """The backend responded, but did not accept this report."""

    def __init__(self, status: int):
        self.status = status
        if status == 400:
            reason = "invalid_report"
        elif status == 413:
            reason = "payload_too_large"
        elif status == 429:
            reason = "rate_limited"
        elif 500 <= status <= 599:
            reason = "server_error"
        elif 300 <= status <= 399:
            reason = "redirect_rejected"
        else:
            reason = "unexpected_status"
        super().__init__(reason)


async def request(hass, url: str, key: str, method: str, path: str, payload=None) -> None:
    """Send one request without retries, redirects, or logging sensitive data."""
    session = async_get_clientsession(hass)
    headers = {"X-Integration-Key": key}
    body = None
    if payload is not None:
        body = _encode_report(payload)
        headers["Content-Type"] = "application/json"
    try:
        async with session.request(
            method,
            f"{url.rstrip('/')}{path}",
            headers=headers,
            data=body,
            timeout=ClientTimeout(total=30),
            allow_redirects=False,
        ) as response:
            if response.status == 204:
                return
            if response.status == 401:
                raise InvalidKeyError
            # Do not read or log an untrusted error body: status is enough to
            # distinguish invalid data from throttling or a temporary outage.
            raise BackendResponseError(response.status)
    except asyncio.TimeoutError:
        raise BackendUnavailableError("timeout") from None
    except ClientSSLError:
        raise BackendUnavailableError("tls_error") from None
    except ClientError:
        raise BackendUnavailableError("connection_error") from None


def _encode_report(payload: dict) -> bytes:
    """Measure the exact wire bytes; oversized snapshots still send a lifecheck."""
    body = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(body) <= MAX_REPORT_BYTES:
        return body

    error = (f"Report prekročil limit {MAX_REPORT_BYTES} bajtov ({len(body)} bajtov); "
             f"vynechané entity: {len(payload['monitoredEntities'])}, inventár: {len(payload['inventory'])}.")
    if payload.get("errorMessage"):
        error += " " + payload["errorMessage"][:2048]
    reduced = {**payload, "isComplete": False, "errorMessage": error, "monitoredEntities": [], "inventory": []}
    _LOGGER.warning("BlackLabs Watchdog report prekročil 1 MiB; odosiela sa neúplný lifecheck bez entít a inventára")
    return json.dumps(reduced, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
