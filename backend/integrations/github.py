"""GitHub Actions client: read workflow runs, dispatch a workflow.

Two operations only, matching what the owner surface offers:

* ``list_runs``     GET  /repos/{repo}/actions/runs
* ``dispatch``      POST /repos/{repo}/actions/workflows/{workflow_id}/dispatches

The token is a classic ``GITHUB_TOKEN``-style PAT or a fine-grained token with
``actions: read`` (plus ``actions: write`` for dispatch). It is read from the
environment by ``config.load`` and is never returned by any method here; the
``redact`` callable passed in scrubs it from any upstream message before that
message can reach a response.
"""
from __future__ import annotations

from urllib.parse import quote

from .http import (ALLOWED_API_HOSTS, HttpResponse, IntegrationError, Transport,
                   UrllibTransport, assert_https_host, retry_after_seconds)

API_VERSION = "2022-11-28"
USER_AGENT = "waha-integrations/1.0"


def _quote_repo(repo):
    """``owner/name`` -> URL-safe path segment. A slash inside either half would
    silently turn the request into a different repository, so refuse it."""
    parts = str(repo).split("/")
    if len(parts) != 2 or not all(parts):
        raise IntegrationError("صيغة GITHUB_REPO يجب أن تكون owner/name.",
                               code="invalid_config")
    return "/".join(quote(part, safe="") for part in parts)


class GitHubClient:
    def __init__(self, settings, redact, transport: Transport = None, timeout: int = 15):
        self.settings = settings
        self.redact = redact
        self.transport = transport or UrllibTransport()
        self.timeout = timeout

    # -- plumbing ---------------------------------------------------------
    def _headers(self):
        return {
            "Authorization": f"Bearer {self.settings.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": USER_AGENT,
        }

    def _url(self, path):
        url = f"{self.settings.api_base}{path}"
        host = assert_https_host(url, ALLOWED_API_HOSTS)
        return url, host

    def _error(self, response: HttpResponse):
        payload = response.json() if isinstance(response, HttpResponse) else None
        detail = ""
        if isinstance(payload, dict):
            detail = str(payload.get("message") or "")
        message = self.redact(detail or f"استجابة {response.status} من GitHub.")
        mapping = {
            401: ("ghp_unauthorized", "رمز GitHub مرفوض أو منتهي."),
            403: ("github_forbidden", "GitHub رفض الطلب (صلاحية أو حدّ طلبات)."),
            404: ("github_not_found", "المستودع أو الـ workflow غير موجود لهذا الرمز."),
            422: ("github_unprocessable", "GitHub رفض الطلب؛ تحقق من الـ ref أو الـ workflow."),
        }
        if response.status in mapping:
            code, fallback = mapping[response.status]
            raise IntegrationError(fallback if not detail else f"{fallback} ({message})",
                                   code=code, status=response.status)
        if response.status == 429 or response.status >= 500:
            raise IntegrationError(message, code="github_unavailable",
                                   status=response.status,
                                   retry_after=retry_after_seconds(response))
        raise IntegrationError(message, code="github_error", status=response.status)

    def _require_configured(self):
        """Checked *before* any path is built.

        Ordering matters and a test pins it: with an empty ``GITHUB_REPO`` the
        path builder raises ``invalid_config`` first, which would tell the
        operator "your request is malformed" when the truth is "this server was
        never given a token" -- a 400 instead of a 503, and the wrong fix.
        """
        if not self.settings.configured:
            raise IntegrationError("تكامل GitHub غير مُهيّأ على الخادم.",
                                   code="not_configured", status=503)

    def _request(self, method, path, body=None):
        self._require_configured()
        url, _host = self._url(path)
        response = self.transport.request(method, url, headers=self._headers(),
                                          body=body, timeout=self.timeout)
        if not response.ok:
            self._error(response)
        return response

    # -- operations -------------------------------------------------------
    def list_runs(self, limit=25, branch=None, workflow=None):
        """Latest workflow runs, newest first, normalised to a stable shape."""
        self._require_configured()
        limit = max(1, min(100, int(limit)))
        query = f"?per_page={limit}"
        if branch:
            query += f"&branch={quote(str(branch), safe='')}"
        response = self._request("GET", f"/repos/{_quote_repo(self.settings.repo)}"
                                        f"/actions/runs{query}")
        payload = response.json()
        if not isinstance(payload, dict) or "workflow_runs" not in payload:
            raise IntegrationError("استجابة غير متوقعة من GitHub.", code="bad_upstream_payload")
        runs = [normalize_run(run) for run in payload.get("workflow_runs") or []
                if isinstance(run, dict)]
        if workflow:
            runs = [run for run in runs
                    if workflow in (run["name"], str(run["run_number"]))]
        return {"provider": "github", "source": "GITHUB_API", "items": runs,
                "count": len(runs), "total_count": payload.get("total_count"),
                "limit": limit, "branch": branch or None}

    def dispatch(self, workflow_id=None, ref="main", inputs=None):
        """Trigger ``workflow_dispatch``. Returns 204 upstream, so there is no body
        to echo -- we report what was asked for, and only that."""
        self._require_configured()
        target = str(workflow_id or self.settings.workflow or "").strip()
        if not target:
            raise IntegrationError("لم يُحدَّد workflow؛ اضبط GITHUB_WORKFLOW_ID.",
                                   code="invalid_config")
        ref = str(ref or "main").strip()
        if not ref or len(ref) > 200:
            raise IntegrationError("ref غير صالح.", code="invalid_request")
        payload = {"ref": ref}
        if inputs:
            if not isinstance(inputs, dict):
                raise IntegrationError("inputs يجب أن تكون كائناً.", code="invalid_request")
            payload["inputs"] = {str(k): v for k, v in list(inputs.items())[:20]}
        response = self._request(
            "POST",
            f"/repos/{_quote_repo(self.settings.repo)}/actions/workflows/"
            f"{quote(target, safe='')}/dispatches",
            body=payload)
        return {"provider": "github", "source": "GITHUB_API", "dispatched": True,
                "workflow": target, "ref": ref, "status": response.status}


def normalize_run(run):
    """Keep only the fields the UI shows. Dropping the rest is deliberate: the raw
    run object embeds ``head_commit`` with author e-mail, and this response goes
    to a browser."""
    return {
        "id": run.get("id"),
        "run_number": run.get("run_number"),
        "name": run.get("name"),
        "title": run.get("display_title"),
        "status": run.get("status"),
        "conclusion": run.get("conclusion"),
        "branch": run.get("head_branch"),
        "sha": (run.get("head_sha") or "")[:12] or None,
        "event": run.get("event"),
        "created_at": run.get("created_at"),
        "updated_at": run.get("updated_at"),
        "url": run.get("html_url"),
    }
