"""Transport seam for the integration clients.

``IntegrationService`` and both vendor clients talk to a ``Transport``, never to
``urllib`` directly. That is what lets the entire surface -- including the error
paths and the redaction -- run under a fake in CI with no network and no secrets,
and it is the same reason ``agent/`` receives a ``provider_factory`` instead of
importing a vendor SDK.

The fixed vendor APIs are hostname-allowlisted. The Vercel deploy hook is checked
for public DNS addresses and those exact addresses are pinned to the HTTPS
connection, so DNS cannot change between validation and use (DNS rebinding).
"""
from __future__ import annotations

import http.client
import ipaddress
import json as _json
import socket
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, Optional
from urllib.parse import urlsplit

from .config import normalize_hostname

# Fixed vendor hosts. Deploy hooks are also issued under api.vercel.com, and
# Render's hook lives on api.render.com with its secret in the *query string* --
# which is why the whole URL, not just a key, goes into ``config.secrets()``.
# Drive needs two hosts because an OAuth refresh is a different origin from the API.
ALLOWED_API_HOSTS = frozenset({"api.github.com", "api.vercel.com", "api.render.com",
                               "www.googleapis.com", "oauth2.googleapis.com"})
MAX_RESPONSE_BYTES = 512 * 1024

# Failures that happen *before* an HTTP response exists. None of them is a verdict
# on the credential: the token was never sent, or never answered. Callers that
# would report "your token is bad" must consult this set first -- a connection cut
# before any response produces exactly the same empty result as a wrong token, and
# the two have opposite fixes. See ``scripts/integrations_live_check.py``, which
# reports these as BLOCKED, not FAIL.
NETWORK_FAILURE_CODES = frozenset({"upstream_unreachable", "upstream_timeout",
                                   "dns_failed", "pre_http_network_failure"})

# The connection ended before any HTTP response existed: the peer closed the TLS
# handshake, reset it, refused it, or vanished mid-request. The socket cannot say
# *why* -- a middlebox, a firewall and an upstream that simply went away are
# identical from here -- so this code names the observable fact and not a cause.
# That fact is the only one the credential question needs, which is why the name
# states when the failure happened and stops there.
_PRE_RESPONSE_CLOSE_ERRORS = (ssl.SSLEOFError, ssl.SSLZeroReturnError,
                              ConnectionResetError, ConnectionRefusedError,
                              http.client.RemoteDisconnected)


def _network_failure_code(error):
    """Classify a transport-level failure that produced no HTTP response."""
    return ("pre_http_network_failure" if isinstance(error, _PRE_RESPONSE_CLOSE_ERRORS)
            else "upstream_unreachable")


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
    non-2xx -- they return the response and let the client decide.

    ``resolved_addresses`` is supplied only after a public-address check. A real
    transport must use those IPs for the socket while retaining the URL hostname
    for the HTTP Host header, TLS SNI and certificate verification.
    """

    def request(self, method, url, headers=None, body=None, timeout=15,
                resolved_addresses=None) -> HttpResponse:
        raise NotImplementedError


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection that dials checked IPs but verifies the original hostname."""

    def __init__(self, host, port, addresses, timeout=15, context=None):
        self.addresses = validate_public_addresses(addresses)
        if context is None:
            context = ssl.create_default_context()
            context.set_alpn_protocols(["http/1.1"])
        super().__init__(host, port=port, timeout=timeout, context=context)

    def connect(self):
        # No proxy/tunnel is used here: that would reintroduce a second resolver
        # between the validation and the actual destination.
        if self._tunnel_host:
            raise OSError("pinned HTTPS does not support tunneling")
        last_error = None
        for address in self.addresses:
            raw_socket = None
            try:
                raw_socket = socket.create_connection((address, self.port), self.timeout)
                self.sock = self._context.wrap_socket(raw_socket, server_hostname=self.host)
                return
            except (OSError, ssl.SSLError) as error:
                last_error = error
                if raw_socket is not None:
                    raw_socket.close()
                self.sock = None
        if last_error is not None:
            raise last_error
        raise OSError("no public addresses were supplied")


class UrllibTransport(Transport):
    """The real transport. Redirects are disabled on purpose: an integration
    endpoint has no business following a hop to somewhere else."""

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    def __init__(self, opener=None):
        self._opener = opener or urllib.request.build_opener(self._NoRedirect)

    def request(self, method, url, headers=None, body=None, timeout=15,
                resolved_addresses=None) -> HttpResponse:
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else _json.dumps(body).encode("utf-8")
        if resolved_addresses is not None:
            return self._request_pinned(method, url, headers, data, timeout,
                                        resolved_addresses)
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
                                   code=_network_failure_code(error.reason)) from error
        except (socket.timeout, TimeoutError) as error:
            raise IntegrationError("انتهت مهلة الاتصال بالخدمة.",
                                   code="upstream_timeout") from error

    @staticmethod
    def _request_pinned(method, url, headers, body, timeout, addresses):
        # Validate again at the transport boundary so a caller cannot accidentally
        # pass an unchecked or empty address list to the connection.
        host = assert_https_host(url)
        parts = urlsplit(url)
        port = parts.port or 443
        target = parts.path or "/"
        if parts.query:
            target += "?" + parts.query
        checked_addresses = validate_public_addresses(addresses)
        connection = _PinnedHTTPSConnection(host, port, checked_addresses, timeout=timeout)
        request_headers = {key: value for key, value in dict(headers or {}).items()
                           if str(key).lower() != "host"}
        try:
            connection.request(method.upper(), target, body=body, headers=request_headers)
            response = connection.getresponse()
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            return HttpResponse(response.status, dict(response.getheaders()), raw)
        except (socket.timeout, TimeoutError) as error:
            raise IntegrationError("انتهت مهلة الاتصال بالخدمة.",
                                   code="upstream_timeout") from error
        except (OSError, http.client.HTTPException, ssl.SSLError) as error:
            raise IntegrationError("تعذّر الوصول إلى الخدمة عبر عنوان DNS المثبّت.",
                                   code=_network_failure_code(error)) from error
        finally:
            connection.close()


def assert_https_host(url, allowed_hosts=None):
    """Require HTTPS, a DNS-safe hostname, no userinfo, and the standard port.

    Returns the canonical host. Raises ``IntegrationError`` otherwise.
    """
    try:
        parts = urlsplit(str(url))
        scheme = parts.scheme.lower()
        port = parts.port
        raw_host = parts.hostname
    except (TypeError, ValueError):
        raise IntegrationError("رابط غير صالح.", code="invalid_target") from None
    if scheme != "https":
        raise IntegrationError("الربط مسموح عبر HTTPS فقط.", code="insecure_target")
    if (not raw_host or not parts.netloc or parts.username is not None
            or parts.password is not None or parts.fragment):
        raise IntegrationError("رابط غير صالح.", code="invalid_target")
    if port not in (None, 443):
        raise IntegrationError("الربط مسموح على منفذ HTTPS القياسي فقط.",
                               code="invalid_target")
    host = normalize_hostname(raw_host)
    if host is None:
        raise IntegrationError("اسم النطاق غير صالح.", code="invalid_target")
    if allowed_hosts is not None:
        allowed = {normalize_hostname(item) for item in allowed_hosts}
        if host not in allowed:
            raise IntegrationError(f"النطاق {host} ليس ضمن النطاقات المسموحة.",
                                   code="host_not_allowed")
    return host


def default_resolver(host):
    """Return all TCP A/AAAA answers; the caller validates before any connect."""
    try:
        answers = socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM,
                                     socket.IPPROTO_TCP)
    except OSError as error:
        raise IntegrationError("تعذّر حلّ اسم النطاق.", code="dns_failed") from error
    return tuple(dict.fromkeys(str(info[4][0]) for info in answers))


def validate_public_addresses(addresses):
    """Canonicalize and require a non-empty set of globally routable IPs."""
    if addresses is None:
        addresses = ()
    if isinstance(addresses, (str, bytes)):
        addresses = (addresses,)
    normalized = []
    for raw in addresses:
        try:
            ip = ipaddress.ip_address(str(raw).split("%", 1)[0])
        except ValueError as error:
            raise IntegrationError("عنوان DNS غير صالح.", code="invalid_target") from error
        if not ip.is_global:
            raise IntegrationError("هذا العنوان داخلي أو محجوز؛ الربط للعموم فقط.",
                                   code="internal_address_blocked")
        address = ip.compressed
        if address not in normalized:
            normalized.append(address)
    if not normalized:
        raise IntegrationError("تعذّر حلّ اسم النطاق إلى عنوان عام.", code="dns_failed")
    return tuple(normalized)


def resolve_public_addresses(host, resolver=None):
    """Resolve once, reject every non-public answer, and return the exact IPs to pin."""
    resolve = resolver or default_resolver
    try:
        addresses = resolve(host)
    except IntegrationError:
        raise
    except OSError as error:
        raise IntegrationError("تعذّر حلّ اسم النطاق.", code="dns_failed") from error
    return validate_public_addresses(addresses)


def assert_public_host(host, resolver=None):
    """Compatibility guard: resolve and reject any internal/reserved answer."""
    resolve_public_addresses(host, resolver)
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
