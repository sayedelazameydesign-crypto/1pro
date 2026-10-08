"""Google Drive client: read the owner's folder, drop a file into it.

Two operations, one per direction, and nothing else:

* ``list_files``   GET  {api_base}/files?q='folder' in parents&...
* ``upload_text``  POST {upload_base}/files?uploadType=media&name=...

Why a token *triplet* and not an SDK. A service account would be the cleanest
server credential, but the owner's files live in a personal Drive and Google only
lets a service account write there by sharing the folder with the generated
address -- an extra manual step that silently fails when skipped. The OAuth
refresh token from the owner's own consent is one paste and needs no sharing, so
that is what ``config`` accepts; ``GOOGLE_DRIVE_ACCESS_TOKEN`` remains as the
short-lived trial path, useful for a smoke test that must not outlive the day.

The client mints a fresh access token per invocation and caches it for its
lifetime, because Drive's answer to an expired token is a 401 that reads exactly
like a revoked grant. Distinguishing the two is the difference between "reconnect"
and "wait an hour", so the expiry is tracked rather than guessed.
"""
from __future__ import annotations

import json as _json
import time
import urllib.parse

from .http import (ALLOWED_API_HOSTS, HttpResponse, IntegrationError, Transport,
                   UrllibTransport, assert_https_host, retry_after_seconds)

USER_AGENT = "waha-integrations/1.0"
# The fields the portal paints. Deliberately no ``owners``, no ``lastModifyingUser``,
# no ``permissions``: this response goes to a browser, and Drive embeds addresses
# and photo URLs in those blocks.
LIST_FIELDS = ("files(id,name,mimeType,modifiedTime,createdTime,size,webViewLink)"
               ",nextPageToken,incompleteSearch")
# A freshly minted token still has ~59 minutes left. Refreshing a minute early
# keeps a slow request from being signed with a token that expires in flight.
TOKEN_SAFETY_MARGIN_SECONDS = 60


class DriveClient:
    def __init__(self, settings, redact, transport: Transport = None, timeout: int = 15,
                 clock=time.time):
        self.settings = settings
        self.redact = redact
        self.transport = transport or UrllibTransport()
        self.timeout = timeout
        self.clock = clock
        self._token = None
        self._expires_at = 0.0

    # -- credentials -------------------------------------------------------
    def _require_configured(self):
        if not self.settings.configured:
            raise IntegrationError(
                "تكامل Google Drive غير مُهيّأ على الخادم "
                "(GOOGLE_DRIVE_ACCESS_TOKEN أو ثلاثي OAuth).",
                code="not_configured", status=503)

    def _request_token(self):
        """Exchange the refresh triple for a short-lived access token."""
        settings = self.settings
        assert_https_host(settings.token_uri, ALLOWED_API_HOSTS)
        form = urllib.parse.urlencode({
            "client_id": settings.client_id,
            "client_secret": settings.client_secret,
            "refresh_token": settings.refresh_token,
            "grant_type": "refresh_token",
        }).encode("utf-8")
        response = self.transport.request(
            "POST", settings.token_uri,
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "Accept": "application/json", "User-Agent": USER_AGENT},
            body=form, timeout=self.timeout)
        payload = response.json() if isinstance(response, HttpResponse) else None
        if not response.ok:
            detail = ""
            if isinstance(payload, dict):
                detail = str(payload.get("error_description") or payload.get("error") or "")
            raise IntegrationError(
                self.redact(detail or f"تعذّر تجديد رمز Drive (استجابة "
                                      f"{response.status})."),
                code="drive_token_failed", status=response.status)
        token = (payload or {}).get("access_token") if isinstance(payload, dict) else None
        if not token:
            raise IntegrationError("استجابة غير متوقعة من خادم رموز Google.",
                                   code="bad_upstream_payload")
        try:
            lifetime = int((payload or {}).get("expires_in") or 3600)
        except (TypeError, ValueError):
            lifetime = 3600
        self._token = str(token)
        self._expires_at = self.clock() + max(0, lifetime - TOKEN_SAFETY_MARGIN_SECONDS)
        return self._token

    def _access_token(self):
        """The bearer to sign with: the configured one, or a freshly minted copy.

        ``GOOGLE_DRIVE_ACCESS_TOKEN`` wins when present, which is what makes the
        trial path behave like the trial path: the server uses exactly what the
        operator pasted and never tries to refresh it.
        """
        settings = self.settings
        if settings.access_token:
            return settings.access_token
        if self._token and self.clock() < self._expires_at:
            return self._token
        return self._request_token()

    def _headers(self):
        return {"Authorization": f"Bearer {self._access_token()}",
                "Accept": "application/json", "User-Agent": USER_AGENT}

    def _error(self, response: HttpResponse):
        payload = response.json() if isinstance(response, HttpResponse) else None
        detail = ""
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                detail = str(error.get("message") or error.get("status") or "")
            elif error:
                detail = str(error)
        message = self.redact(detail or f"استجابة {response.status} من Google Drive.")
        mapping = {
            401: ("drive_unauthorized", "رمز Google Drive مرفوض أو منتهي."),
            403: ("drive_forbidden", "Drive رفض الطلب (صلاحية أو حصة منخفضة)."),
            404: ("drive_not_found", "المجلد أو الملف غير موجود لهذا الحساب."),
        }
        if response.status in mapping:
            code, fallback = mapping[response.status]
            raise IntegrationError(fallback if not detail else f"{fallback} ({message})",
                                   code=code, status=response.status)
        if response.status == 429 or response.status >= 500:
            raise IntegrationError(message, code="drive_unavailable",
                                   status=response.status,
                                   retry_after=retry_after_seconds(response))
        raise IntegrationError(message, code="drive_error", status=response.status)

    # -- plumbing ----------------------------------------------------------
    def _get(self, url):
        assert_https_host(url, ALLOWED_API_HOSTS)
        response = self.transport.request("GET", url, headers=self._headers(),
                                          timeout=self.timeout)
        if not response.ok:
            self._error(response)
        return response

    def _folder_filter(self, folder_id):
        folder = str(folder_id or self.settings.folder_id or "").strip()
        if not folder:
            return ""
        # A quote inside the id would end the string and let the rest of the query
        # say something else. Drive ids never contain one, so refusing is free.
        if "'" in folder:
            raise IntegrationError("GOOGLE_DRIVE_FOLDER_ID غير صالح.", code="invalid_config")
        clause = f"'{folder}' in parents and trashed = false"
        return "&q=" + urllib.parse.quote(clause, safe="")

    # -- operations --------------------------------------------------------
    def list_files(self, limit=25, folder_id=None):
        """Newest files in the configured folder (or My Drive when unset)."""
        self._require_configured()
        limit = max(1, min(100, int(limit)))
        url = (f"{self.settings.api_base}/files?pageSize={limit}"
               f"{self._folder_filter(folder_id)}"
               f"&orderBy={urllib.parse.quote('modifiedTime desc', safe='')}"
               f"&fields={urllib.parse.quote(LIST_FIELDS, safe='')}")
        payload = self._get(url).json()
        if not isinstance(payload, dict) or "files" not in payload:
            raise IntegrationError("استجابة غير متوقعة من Google Drive.",
                                   code="bad_upstream_payload")
        items = [normalize_file(entry) for entry in payload.get("files") or []
                 if isinstance(entry, dict)]
        return {"provider": "drive", "source": "GOOGLE_DRIVE_API", "items": items,
                "count": len(items), "limit": limit,
                "folder_id": str(folder_id or self.settings.folder_id or "") or None,
                "has_more": bool(payload.get("nextPageToken"))}

    def upload_text(self, name, text, mime_type="text/markdown", folder_id=None):
        """Create one text file. ``uploadType=media`` is the whole design: a JSON
        metadata block would need multipart, and every field the portal offers
        (name, parents, mimeType) has a query-parameter form.

        The response echoes id and link only. Drive returns the full ``File``
        resource -- permissions, capabilities, the owner's e-mail -- and none of it
        belongs in a browser payload.
        """
        self._require_configured()
        name = str(name or "").strip()
        if not name or len(name) > 200 or "/" in name or "\x00" in name:
            raise IntegrationError("اسم الملف غير صالح.", code="invalid_request")
        body = ("" if text is None else str(text)).encode("utf-8")
        if len(body) > 512 * 1024:
            raise IntegrationError("المحتوى أكبر من 512KB؛ ارفع ملفًا من المستودع.",
                                   code="invalid_request")
        query = {"uploadType": "media", "name": name,
                 "responseType": "json"}
        folder = str(folder_id or self.settings.folder_id or "").strip()
        if folder:
            if "'" in folder:
                raise IntegrationError("GOOGLE_DRIVE_FOLDER_ID غير صالح.",
                                       code="invalid_config")
            query["parents"] = folder
        url = (f"{self.settings.upload_base}/files?"
               + urllib.parse.urlencode(query))
        assert_https_host(url, ALLOWED_API_HOSTS)
        headers = {"Authorization": f"Bearer {self._access_token()}",
                   "Accept": "application/json", "User-Agent": USER_AGENT,
                   "Content-Type": _safe_mime(mime_type)}
        response = self.transport.request("POST", url, headers=headers, body=body,
                                          timeout=self.timeout)
        if not response.ok:
            self._error(response)
        payload = response.json()
        if not isinstance(payload, dict) or not payload.get("id"):
            raise IntegrationError("استجابة غير متوقعة من Google Drive.",
                                   code="bad_upstream_payload")
        return {"provider": "drive", "source": "GOOGLE_DRIVE_API", "created": True,
                "status": response.status, "file": normalize_file(payload)}


def _safe_mime(raw):
    """Accept only a plain ``type/subtype``; a stray header separator is rejected.

    The value goes into a request header, so a newline inside it would be header
    injection -- small, but the fix is one regex and the failure is silent.
    """
    import re
    value = str(raw or "").strip()
    if re.fullmatch(r"[a-zA-Z0-9.+-]{1,60}/[a-zA-Z0-9.+-]{1,120}", value):
        return value
    return "text/plain"


def normalize_file(entry):
    """Fields the portal shows, and nothing Drive can use to identify a person."""
    entry = entry if isinstance(entry, dict) else {}
    try:
        size = int(entry.get("size")) if entry.get("size") not in (None, "") else None
    except (TypeError, ValueError):
        size = None
    return {
        "id": entry.get("id"),
        "name": entry.get("name"),
        "mime_type": entry.get("mimeType"),
        "modified_at": entry.get("modifiedTime"),
        "created_at": entry.get("createdTime"),
        "size": size,
        "url": entry.get("webViewLink"),
    }
