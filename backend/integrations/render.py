"""Render client: read the deploy log, fire a deploy hook.

Two operations only, mirroring ``vercel.py`` one for one:

* ``list_deploys``        GET  {api_base}/services/{service_id}/deploys?limit=..
* ``trigger_deploy_hook`` POST {RENDER_DEPLOY_HOOK_URL}

The split is the same on purpose: listing proves *what is live* and needs
``RENDER_API_KEY``; the hook is the only thing that changes production and needs no
key at all, because the secret is the URL. A leaked read key therefore cannot
deploy, and a leaked hook URL cannot read the deploy log.

Render's hook URL carries its secret in a ``?key=`` query parameter, which is a
worse leak than Vercel's path segment: query strings end up in proxy logs and
browser history far more readily than path segments do. So the redactor gets the
whole URL, and nothing in this module ever puts it in a return value.
"""
from __future__ import annotations

from urllib.parse import quote

from .http import (ALLOWED_API_HOSTS, HttpResponse, IntegrationError, Transport,
                   UrllibTransport, assert_https_host, resolve_public_addresses,
                   retry_after_seconds)

RENDER_HOOK_HOST = "api.render.com"
USER_AGENT = "waha-integrations/1.0"

class RenderClient:
    def __init__(self, settings, redact, transport: Transport = None, timeout: int = 15,
                 resolver=None):
        self.settings = settings
        self.redact = redact
        self.transport = transport or UrllibTransport()
        self.timeout = timeout
        # Injected so the hook guard can be exercised without real DNS, exactly as
        # in VercelClient; see http.resolve_public_addresses for why that matters.
        self.resolver = resolver

    # -- plumbing ---------------------------------------------------------
    def _headers(self):
        return {"Authorization": f"Bearer {self.settings.api_key}",
                "Accept": "application/json",
                "User-Agent": USER_AGENT}

    def _require_configured(self):
        if not self.settings.configured:
            raise IntegrationError("تكامل Render غير مُهيّأ على الخادم.",
                                   code="not_configured", status=503)

    def _error(self, response: HttpResponse):
        payload = response.json() if isinstance(response, HttpResponse) else None
        detail = ""
        if isinstance(payload, dict):
            detail = str(payload.get("message") or payload.get("error") or "")
        elif isinstance(payload, list) and payload and isinstance(payload[0], dict):
            detail = str(payload[0].get("message") or "")
        message = self.redact(detail or f"استجابة {response.status} من Render.")
        mapping = {
            401: ("render_unauthorized", "مفتاح Render مرفوض أو منتهي."),
            403: ("render_forbidden", "Render رفض الطلب (صلاحية أو حساب خاطئ)."),
            404: ("render_not_found", "الـ service غير موجود لهذا المفتاح."),
        }
        if response.status in mapping:
            code, fallback = mapping[response.status]
            raise IntegrationError(fallback if not detail else f"{fallback} ({message})",
                                   code=code, status=response.status)
        if response.status == 429 or response.status >= 500:
            raise IntegrationError(message, code="render_unavailable",
                                   status=response.status,
                                   retry_after=retry_after_seconds(response))
        raise IntegrationError(message, code="render_error", status=response.status)

    def _request(self, method, path, body=None):
        self._require_configured()
        url = f"{self.settings.api_base}{path}"
        assert_https_host(url, ALLOWED_API_HOSTS)
        response = self.transport.request(method, url, headers=self._headers(),
                                          body=body, timeout=self.timeout)
        if not response.ok:
            self._error(response)
        return response

    # -- operations -------------------------------------------------------
    def list_deploys(self, limit=25):
        """Latest deploys for the configured service, newest first.

        Render answers a bare JSON array of ``{"deploy": {...}}`` wrappers, so the
        unwrap is the whole parse. An object-shaped answer is a contract break and
        is reported as ``bad_upstream_payload`` rather than read as an empty log:
        "no deploys" and "I could not read the answer" are different claims.
        """
        self._require_configured()
        limit = max(1, min(100, int(limit)))
        service = quote(self.settings.service_id, safe="")
        response = self._request("GET", f"/services/{service}/deploys?limit={limit}")
        payload = response.json()
        if not isinstance(payload, list):
            raise IntegrationError("استجابة غير متوقعة من Render.",
                                   code="bad_upstream_payload")
        items = [normalize_deploy(entry.get("deploy"))
                 for entry in payload if isinstance(entry, dict)]
        return {"provider": "render", "source": "RENDER_API", "items": items,
                "count": len(items), "limit": limit,
                "service_id": self.settings.service_id}

    def trigger_deploy_hook(self):
        """Fire Render's deploy hook without a DNS check/use race.

        Same guard as Vercel's hook: HTTPS, the vendor host, resolved once, every
        answer required to be globally routable, and those exact addresses pinned
        to the socket. The URL itself is never echoed.
        """
        hook = self.settings.deploy_hook
        if not hook:
            raise IntegrationError("RENDER_DEPLOY_HOOK_URL غير مضبوط على الخادم.",
                                   code="not_configured", status=503)
        host = assert_https_host(hook, {RENDER_HOOK_HOST})
        addresses = resolve_public_addresses(host, self.resolver)
        response = self.transport.request("POST", hook,
                                          headers={"User-Agent": USER_AGENT},
                                          body=b"{}", timeout=self.timeout,
                                          resolved_addresses=addresses)
        if not response.ok:
            # Render answers a hook with plain text ("Deploy queued"), not JSON.
            message = self.redact(response.text(300) or f"استجابة {response.status}.")
            raise IntegrationError(message, code="deploy_hook_failed",
                                   status=response.status,
                                   retry_after=retry_after_seconds(response))
        return {"provider": "render", "source": "DEPLOY_HOOK", "triggered": True,
                "status": response.status,
                "message": self.redact(response.text(120)) or None}


def normalize_deploy(deploy):
    """The fields the UI shows, nothing else.

    The raw deploy embeds the commit author's e-mail and the whole build log
    location, and this response goes to a browser, so the commit is narrowed to
    ``ref`` + a 12-char ``sha`` + the first line of the message.
    """
    deploy = deploy if isinstance(deploy, dict) else {}
    commit = deploy.get("commit") if isinstance(deploy.get("commit"), dict) else {}
    status = deploy.get("status")
    message = str(commit.get("message") or "").strip().splitlines()
    return {
        "id": deploy.get("id"),
        # The raw word, unchanged: the page decides whether it is good or bad, so
        # a status Render invents tomorrow reads as unknown there instead of being
        # silently re-labelled as fine by a mapping written today.
        "state": status,
        "created_at": deploy.get("createdAt"),
        "branch": commit.get("ref"),
        # ``commit.id`` is Render's commit sha; ``ref`` is the branch name. They are
        # easy to confuse from the Vercel shape, where the sha sits in ``meta``.
        "sha": (commit.get("id") or "")[:12] or None,
        "title": (message[0][:120] if message else None),
        "url": commit.get("url"),
    }
