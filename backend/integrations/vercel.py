"""Vercel client: read deployments, fire a deploy hook.

Two operations only:

* ``list_deployments``  GET  {api_base}/v6/deployments?projectId=..&limit=..
* ``trigger_deploy_hook`` POST {DEPLOY_HOOK_URL}

They are separate on purpose. Listing needs ``VERCEL_TOKEN`` and reads project
state; the hook needs no token at all -- the secret *is* the URL -- and it is the
operation that actually changes production. Keeping them apart means a leaked
read token cannot deploy, and a leaked hook URL cannot read.
"""
from __future__ import annotations

from urllib.parse import quote

from .http import (ALLOWED_API_HOSTS, IntegrationError, assert_https_host,
                   resolve_public_addresses, retry_after_seconds)
from .http import Transport, UrllibTransport  # noqa: F401  (re-exported for callers)


class VercelClient:
    def __init__(self, settings, redact, transport: Transport = None, timeout: int = 15,
                 resolver=None):
        self.settings = settings
        self.redact = redact
        self.transport = transport or UrllibTransport()
        self.timeout = timeout
        # Injected so the hook guard can be exercised without real DNS; see
        # http.assert_public_host for why that matters.
        self.resolver = resolver

    def _headers(self):
        return {"Authorization": f"Bearer {self.settings.token}",
                "User-Agent": "waha-integrations/1.0"}

    def _error(self, response):
        payload = response.json()
        detail = ""
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                detail = str(error.get("message") or error.get("code") or "")
            elif error:
                detail = str(error)
        message = self.redact(detail or f"استجابة {response.status} من Vercel.")
        mapping = {
            401: ("vercel_unauthorized", "رمز Vercel مرفوض أو منتهي."),
            403: ("vercel_forbidden", "Vercel رفض الطلب (صلاحية أو فريق خاطئ)."),
            404: ("vercel_not_found", "المشروع غير موجود لهذا الرمز."),
        }
        if response.status in mapping:
            code, fallback = mapping[response.status]
            raise IntegrationError(fallback if not detail else f"{fallback} ({message})",
                                   code=code, status=response.status)
        if response.status == 429 or response.status >= 500:
            raise IntegrationError(message, code="vercel_unavailable",
                                   status=response.status,
                                   retry_after=retry_after_seconds(response))
        raise IntegrationError(message, code="vercel_error", status=response.status)

    def list_deployments(self, limit=25):
        settings = self.settings
        if not settings.configured:
            raise IntegrationError("تكامل Vercel غير مُهيّأ على الخادم.",
                                   code="not_configured", status=503)
        limit = max(1, min(100, int(limit)))
        query = f"projectId={quote(settings.project_id, safe='')}&limit={limit}"
        if settings.team_id:
            query += f"&teamId={quote(settings.team_id, safe='')}"
        url = f"{settings.api_base}/v6/deployments?{query}"
        assert_https_host(url, ALLOWED_API_HOSTS)
        response = self.transport.request("GET", url, headers=self._headers(),
                                          timeout=self.timeout)
        if not response.ok:
            self._error(response)
        payload = response.json()
        if not isinstance(payload, dict) or "deployments" not in payload:
            raise IntegrationError("استجابة غير متوقعة من Vercel.",
                                   code="bad_upstream_payload")
        items = [normalize_deployment(dep) for dep in payload.get("deployments") or []
                 if isinstance(dep, dict)]
        pagination = payload.get("pagination") or {}
        return {"provider": "vercel", "source": "VERCEL_API", "items": items,
                "count": len(items), "limit": limit,
                "has_more": bool(pagination.get("next"))}

    def trigger_deploy_hook(self):
        """Fire Vercel's deploy hook without a DNS check/use race.

        The URL is the credential, so it is restricted to Vercel's generated
        endpoint, resolved once, checked as globally routable, then pinned to the
        HTTPS socket. It is never included in the response.
        """
        hook = self.settings.deploy_hook
        if not hook:
            raise IntegrationError("DEPLOY_HOOK_URL غير مضبوط على الخادم.",
                                   code="not_configured", status=503)
        host = assert_https_host(hook, {"api.vercel.com"})
        addresses = resolve_public_addresses(host, self.resolver)
        response = self.transport.request("POST", hook,
                                          headers={"User-Agent": "waha-integrations/1.0"},
                                          body=b"{}", timeout=self.timeout,
                                          resolved_addresses=addresses)
        if not response.ok:
            # Reuse the mapping, but the hook answers plain text more often than JSON.
            message = self.redact(response.text(300) or f"استجابة {response.status}.")
            raise IntegrationError(message, code="deploy_hook_failed",
                                   status=response.status,
                                   retry_after=retry_after_seconds(response))
        payload = response.json()
        deployment = payload if isinstance(payload, dict) else {}
        # Echo only the id/url/state. The hook response can carry the project's
        # environment variables in some Vercel plans, and this reaches a browser.
        return {"provider": "vercel", "source": "DEPLOY_HOOK", "triggered": True,
                "status": response.status,
                "deployment_id": deployment.get("id") or deployment.get("deploymentId"),
                "deployment_url": deployment.get("url"),
                "state": deployment.get("state") or deployment.get("readyState")}


def normalize_deployment(dep):
    """Fields the UI shows, nothing else. ``meta`` is narrowed to the git refs
    because the raw dict can carry arbitrary project metadata."""
    meta = dep.get("meta") if isinstance(dep.get("meta"), dict) else {}
    created = dep.get("created")
    return {
        "id": dep.get("uid") or dep.get("id"),
        "name": dep.get("name"),
        "url": dep.get("url"),
        "state": dep.get("state") or dep.get("readyState"),
        "target": dep.get("target"),
        "created_at": _iso(created),
        "creator": (dep.get("creator") or {}).get("username")
        if isinstance(dep.get("creator"), dict) else None,
        "branch": meta.get("githubCommitRef"),
        "sha": (meta.get("githubCommitSha") or "")[:12] or None,
    }


def _iso(ms):
    """Vercel returns epoch milliseconds; the UI formats, so convert here once."""
    try:
        import datetime
        return datetime.datetime.fromtimestamp(int(ms) / 1000.0,
                                               datetime.timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None
