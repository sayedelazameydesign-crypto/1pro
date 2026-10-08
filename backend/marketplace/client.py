"""Outbound calls to Vercel: OAuth, SSO, resource events, secret pushes.

Everything here goes through the same ``Transport`` seam the owner integrations
use, so the whole surface runs under a fake in CI with no network and no
credentials. That matters more here than there: these calls happen *inside* an
installation callback, and an integration that cannot be tested without
installing itself is an integration nobody tests.

Three different credentials are in play and they are never mixed:

* **client_id + client_secret** prove *we* are Waha. Used for the two code
  exchanges only.
* **the installation access token** (handed to us in the upsert call) proves
  *this customer's* installation. Used for everything scoped to them.
* **nothing at all** for the JWKS fetch -- it is a public document, and its
  integrity comes from the signature check, not from the transport.

Keeping the third one out of the authenticated group is deliberate: an
attacker who can answer our JWKS request with a key of their own choosing has
already won, so there is no point pretending a bearer token would stop them.
"""
from __future__ import annotations

import json
from urllib.parse import urlencode

from integrations.http import (ALLOWED_API_HOSTS, IntegrationError, assert_https_host,
                               retry_after_seconds)
from integrations.http import Transport, UrllibTransport  # noqa: F401

from .errors import MarketplaceError

USER_AGENT = "waha-marketplace/1.0"

# Upstream statuses that mean "this will not work until a human acts", as
# opposed to "try again" -- the distinction the caller needs in order to answer
# Vercel with a 502 instead of a retry loop.
FATAL_STATUSES = {400: "vercel_bad_request", 401: "vercel_unauthorized",
                  403: "vercel_forbidden", 404: "vercel_not_found",
                  409: "vercel_conflict"}

# The JWKS is a public document on a host of its own, and it is the one place a
# forged response would be catastrophic: whoever answers this request chooses
# the keys every Partner API call is verified against. So the host is pinned to
# exactly one name over HTTPS, with no bearer token to give the illusion that
# authentication protects it -- the signature check is the protection.
MARKETPLACE_HOSTS = frozenset({"marketplace.vercel.com"})


def jwks_fetcher(url, transport: Transport = None, timeout: int = 10) -> bytes:
    """Fetch Vercel's JWKS. Raises ``MarketplaceError`` on anything but a 200.

    Only a success is acceptable, and only once: a truncated or a cached-stale
    key set is not something to fall back from. ``JwksCache`` decides what to do
    when this fails; nothing else should be guessing.
    """
    assert_https_host(url, MARKETPLACE_HOSTS)
    transport = transport or UrllibTransport()
    response = transport.request("GET", url,
                                 headers={"User-Agent": USER_AGENT,
                                          "Accept": "application/json"},
                                 timeout=timeout)
    if not response.ok:
        raise MarketplaceError(f"تعذّر جلب مفاتيح Vercel ({response.status}).",
                               code="jwks_unavailable", status=503)
    return response.body


class MarketplaceClient:
    def __init__(self, settings, redact, transport: Transport = None,
                 timeout: int = 15, resolver=None):
        self.settings = settings
        self.redact = redact
        self.transport = transport or UrllibTransport()
        self.timeout = timeout
        self.resolver = resolver

    # -- helpers -----------------------------------------------------------
    def _url(self, path: str) -> str:
        return f"{self.settings.api_base}{path if path.startswith('/') else '/' + path}"

    def _post(self, path, *, data=None, form=None, token=None, timeout=None):
        url = self._url(path)
        assert_https_host(url, ALLOWED_API_HOSTS)
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if form is not None:
            body = urlencode(form).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif data is not None:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        else:
            body = b""
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return self.transport.request("POST", url, headers=headers, body=body,
                                      timeout=timeout or self.timeout)

    def _get(self, path, *, token=None, timeout=None):
        url = self._url(path)
        assert_https_host(url, ALLOWED_API_HOSTS)
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return self.transport.request("GET", url, headers=headers, timeout=timeout or self.timeout)

    def _put(self, path, *, data=None, token=None, timeout=None):
        url = self._url(path)
        assert_https_host(url, ALLOWED_API_HOSTS)
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json",
                   "Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        body = json.dumps(data or {}, ensure_ascii=False).encode("utf-8")
        return self.transport.request("PUT", url, headers=headers, body=body,
                                      timeout=timeout or self.timeout)

    def _failure(self, response, what):
        """Turn a non-2xx into a redacted MarketplaceError.

        The status is kept as ``upstream_status`` because 403 from Vercel and
        403 from us mean different things to the customer, and collapsing them
        loses the only clue about which side to fix.
        """
        payload = response.json() if response is not None else None
        detail = ""
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                detail = str(error.get("message") or error.get("code") or "")
            elif error:
                detail = str(error)
        message = self.redact(detail or (response.text(300) if response else "")
                              or f"استجابة {response.status} من Vercel.")
        status = response.status
        if status in FATAL_STATUSES:
            raise MarketplaceError(f"{what}: {message}", code=FATAL_STATUSES[status],
                                   status=502)
        raise MarketplaceError(f"{what}: {message}", code="vercel_unavailable", status=504,
                               retry_after=retry_after_seconds(response))

    def _json(self, response, what):
        if not response.ok:
            self._failure(response, what)
        payload = response.json()
        if not isinstance(payload, dict):
            raise MarketplaceError(f"{what}: استجابة غير متوقعة من Vercel.",
                                   code="bad_upstream_payload", status=502)
        return payload

    def _network_guard(self, error, what):
        """Transport exceptions are not verdicts on the credential.

        ``NETWORK_FAILURE_CODES`` exists because a request that never reached
        Vercel looks identical to one Vercel refused, while the fixes are
        opposite. Re-raise as a 504 so Vercel retries instead of failing the
        install.
        """
        if isinstance(error, IntegrationError):
            raise MarketplaceError(f"{what}: {self.redact(error.message)}",
                                   code=error.code, status=504,
                                   retry_after=error.retry_after) from error
        raise

    # -- OAuth2: the install (Redirect URL) flow ---------------------------
    def exchange_oauth_code(self, code, redirect_uri=None):
        """Exchange the one-shot install ``code`` for a long-lived token.

        Form-encoded, because that is what ``/v2/oauth/access_token`` reads --
        posting JSON there returns a 400 whose body does not say why. The
        ``redirect_uri`` must match the console's Redirect URL byte for byte;
        Vercel's 403 for a mismatch names neither value, so the caller passes
        ours in and the deploy doctor compares them before an install is tried.
        """
        if not self.settings.configured:
            raise MarketplaceError("بيانات تكامل Vercel غير مهيّأة.",
                                   code="not_configured", status=503)
        if not code:
            raise MarketplaceError("رمز التثبيت مفقود.", code="missing_code", status=400)
        form = {"client_id": self.settings.client_id,
                "client_secret": self.settings.client_secret,
                "code": code,
                "redirect_uri": redirect_uri or self.settings.redirect_url}
        try:
            response = self._post("/v2/oauth/access_token", form=form)
        except IntegrationError as error:
            self._network_guard(error, "تبادل رمز التثبيت")
        payload = self._json(response, "تبادل رمز التثبيت")
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise MarketplaceError("استجابة Vercel بلا رمز وصول.",
                                   code="bad_upstream_payload", status=502)
        return {"access_token": token,
                "token_type": str(payload.get("token_type") or "Bearer"),
                "installation_id": payload.get("installation_id") or "",
                "user_id": payload.get("user_id") or "",
                "team_id": payload.get("team_id") or "",
                "configuration_id": payload.get("configurationId")
                or payload.get("configuration_id") or ""}

    # -- SSO: the "Open in Provider" (Redirect Login URL) flow -------------
    def exchange_sso_token(self, code, state=None):
        """Exchange a dashboard ``code`` for an OIDC ``id_token``.

        This is the login, not the install: the token identifies the *user*
        opening our dashboard from Vercel and is verified by ``crypto.verify_jwt``
        before a session is minted. ``state`` is echoed back to Vercel because
        they send it and expect it; it is not a credential.
        """
        if not self.settings.configured:
            raise MarketplaceError("بيانات تكامل Vercel غير مهيّأة.",
                                   code="not_configured", status=503)
        if not code:
            raise MarketplaceError("رمز الدخول مفقود.", code="missing_code", status=400)
        data = {"code": code, "state": state or "",
                "client_id": self.settings.client_id,
                "client_secret": self.settings.client_secret}
        try:
            response = self._post("/v1/integrations/sso/token", data=data)
        except IntegrationError as error:
            self._network_guard(error, "تبادل رمز الدخول")
        payload = self._json(response, "تبادل رمز الدخول")
        id_token = payload.get("id_token")
        if not isinstance(id_token, str) or not id_token:
            raise MarketplaceError("استجابة Vercel بلا توكن هوية.",
                                   code="bad_upstream_payload", status=502)
        return id_token

    # -- installation-scoped calls ----------------------------------------
    def dispatch_event(self, access_token, installation_id, event):
        """Tell Vercel something changed on our side (``resource.updated``).

        Without this the store's status stays whatever it was at provisioning
        time, which is how a dashboard ends up showing "ready" forever. A
        failure here is *not* fatal to the request that caused it: the state is
        already committed locally, so the caller logs and carries on rather than
        rolling back a resource that exists.
        """
        if not access_token:
            raise MarketplaceError("لا رمز وصول لهذا التثبيت.", code="not_configured",
                                   status=503)
        try:
            response = self._post(f"/v1/installations/{installation_id}/events",
                                  data={"event": event}, token=access_token)
        except IntegrationError as error:
            self._network_guard(error, "إرسال الحدث")
        if not response.ok:
            self._failure(response, "إرسال الحدث")
        return True

    def update_secrets(self, access_token, installation_id, resource_id, secrets):
        """Push new secret values into Vercel so connected projects get them.

        This is the second half of a rotation. Rotating only in our own database
        would leave every connected project holding the old token, which is the
        one situation worse than not rotating: two credentials, and no way to
        tell which one leaked.
        """
        if not access_token:
            raise MarketplaceError("لا رمز وصول لهذا التثبيت.", code="not_configured",
                                   status=503)
        try:
            response = self._put(f"/v1/installations/{installation_id}/resources/"
                                 f"{resource_id}/secrets", data={"secrets": list(secrets)},
                                 token=access_token)
        except IntegrationError as error:
            self._network_guard(error, "تحديث الأسرار")
        if not response.ok:
            self._failure(response, "تحديث الأسرار")
        return True

    def account_info(self, access_token, installation_id):
        """Re-read the account from Vercel rather than trusting what we stored.

        The account name and contact can change on Vercel's side at any time
        after install, and the upsert payload is a snapshot -- so anything shown
        to a human is re-fetched here, and the stored copy is only a fallback.
        """
        if not access_token:
            raise MarketplaceError("لا رمز وصول لهذا التثبيت.", code="not_configured",
                                   status=503)
        try:
            response = self._get(f"/v1/installations/{installation_id}/account",
                                 token=access_token)
        except IntegrationError as error:
            self._network_guard(error, "قراءة بيانات الحساب")
        return self._json(response, "قراءة بيانات الحساب")
