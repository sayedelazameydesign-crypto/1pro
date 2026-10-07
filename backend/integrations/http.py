"""Transport seam for the integration clients.

``IntegrationService`` and both vendor clients talk to a ``Transport``, never to
``urllib`` directly. That is what lets the entire surface -- including the error
paths and the redaction -- run under a fake in CI with no network and no secrets,
and it is the same reason ``agent/`` receives a ``provider_factory`` instead of
importing a vendor SDK.

The guard here is narrower than the ``web_fetch`` one in ``agent/tools.py``: the
destination is *not* user input, it comes from the operator's environment. So the
allowlist is a fixed set of API hosts plus whatever host the operator put in
``DEPLOY_HOOK_URL``, and the checks are https-only and public-address-only.
"""
from __future__ import annotations

import ipaddress
import json as _json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, Optional
from urllib.parse import urlsplit

# Fixed vendor hosts. A deploy hook lives on an operator-chosen host, so it is
# validated by scheme + public address rather than by membership here.
ALLOWED_API_HOSTS = frozenset({"api.github.com", "api.vercel.com"})
MAX_RESPONSE_BYTES = 512 * 1024


class IntegrationError(Exception):
    """A failure the API layer turns into a JSON error instead of a 500 page."""

    def __init__(self, message, code="integration_error", status=None, retry_after=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.retry_after = retry_after

    def describe(self):
        out = {"error": self.message, "code": self.code}
        if self.status is not None:
            out["upstream_status"] = self.status
        if self.retry_after is not None:
            out["retry_after"] = self.retry_after
        return out


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Dict[str, str] = field(default_factory=dict)
    body: bytes = b""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def header(self, name, default=""):
        lowered = name.lower()
        for key, value in self.headers.items():
            if key.lower() == lowered:
                return value
        return default

    def json(self):
        if not self.body:
            return None
        try:
            return _json.loads(self.body.decode("utf-8", "replace"))
        except (ValueError, UnicodeDecodeError):
            return None

    def text(self, limit=600):
        return self.body.decode("utf-8", "replace")[:limit]


class Transport:
    """The single method a client may call. Implementations must not raise on
    non-2xx -- they return the response and let the client decide."""

    def request(self, method, url, headers=None, body=None, timeout=15) -> HttpResponse:
        raise NotImplementedError


class UrllibTransport(Transport):
    """The real transport. Redirects are disabled on purpose: an integration
    endpoint has no business following a hop to somewhere else."""

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    def __init__(self, opener=None):
        self._opener = opener or urllib.request.build_opener(self._NoRedirect)

    def request(self, method, url, headers=None, body=None, timeout=15) -> HttpResponse:
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else _json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method.upper(),
                                     headers=dict(headers or {}))
        try:
            with self._opener.open(req, timeout=timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                return HttpResponse(response.status, dict(response.headers), raw)
        except urllib.error.HTTPError as error:
            raw = error.read(MAX_RESPONSE_BYTES + 1) if error.fp else b""
            return HttpResponse(error.code, dict(error.headers or {}), raw)
        except urllib.error.URLError as error:
            raise IntegrationError(f"تعذّر الوصول إلى الخدمة: {error.reason}",
                                   code="upstream_unreachable") from error
        except (socket.timeout, TimeoutError) as error:
            raise IntegrationError("انتهت مهلة الاتصال بالخدمة.",
                                   code="upstream_timeout") from error


def assert_https_host(url, allowed_hosts=None):
    """Refuse anything that is not https on an allowed (or explicitly trusted) host.

    Returns the host. Raises ``IntegrationError`` otherwise.
    """
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise IntegrationError("الربط مسموح عبر HTTPS فقط.", code="insecure_target")
    host = parts.hostname or ""
    if not host:
        raise IntegrationError("رابط غير صالح.", code="invalid_target")
    if allowed_hosts is not None and host not in allowed_hosts:
        raise IntegrationError(f"النطاق {host} ليس ضمن النطاقات المسموحة.",
                               code="host_not_allowed")
    return host


def default_resolver(host):
    try:
        return [str(info[4][0]) for info
                in socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)]
    except socket.gaierror as error:
        raise IntegrationError("تعذّر حلّ اسم النطاق.", code="dns_failed") from error


def assert_public_host(host, resolver=None):
    """Same intent as the SSRF guard in ``agent/tools.py``: no loopback, private,
    link-local, reserved or multicast destination. Applied to the operator's own
    ``DEPLOY_HOOK_URL`` so a typo cannot point a deploy at the metadata service.

    The resolver is injectable. That is not test convenience dressed up as
    design: without it, every offline test of the hook path performed a real DNS
    lookup, so the suite depended on the network it claims not to need -- and a
    sandbox without DNS would report a *guard* failure instead of exercising it.
    """
    resolve = resolver or default_resolver
    for raw in resolve(host):
        try:
            ip = ipaddress.ip_address(str(raw).split("%")[0])
        except ValueError as error:
            raise IntegrationError("عنوان غير صالح.", code="invalid_target") from error
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            raise IntegrationError("هذا العنوان داخلي أو محجوز؛ الربط للعموم فقط.",
                                   code="internal_address_blocked")
    return True


def retry_after_seconds(response) -> Optional[int]:
    """Parse Retry-After (delta-seconds form only) for a 429/503 upstream."""
    if response.status not in (429, 503):
        return None
    raw = response.header("Retry-After", "").strip()
    try:
        return max(0, min(3600, int(raw)))
    except ValueError:
        return None
