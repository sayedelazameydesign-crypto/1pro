"""The only object ``app.py`` talks to.

Like ``integrations/service.py``, this module owns the cross-cutting rules so a
route cannot forget one:

1. **Every Partner API call is authenticated before it is parsed.** Vercel signs
   with a key we fetched from Vercel; nothing in the request is trusted to name
   its own key.
2. **The installation in the token must be the installation in the path.** A
   valid token for customer A must never read customer B's resources -- the
   claim and the URL are compared on every call, because either one alone is
   forgeable by the other's holder.
3. **Write calls require the ADMIN role.** Vercel grants ``USER`` to read-only
   members, and provisioning is not a read.
4. **Credentials are described, never emitted, outside the one response Vercel
   requires them in.** The provisioning reply carries secrets because the
   contract says it must; every other view carries a fingerprint.

The webhook path is deliberately the exception to rule 1: it authenticates with
an HMAC of the raw body instead of a JWT, and it answers 200 even when handling
fails, because a non-2xx makes Vercel retry and retries would re-apply an
uninstall.
"""
from __future__ import annotations

import datetime
import json
import secrets
import time
from typing import Callable, Mapping, Optional

from integrations.redact import build_redactor, fingerprint

from . import config as marketplace_config
from .client import MarketplaceClient
from .crypto import (SESSION_PREFIX, STATE_PREFIX, JwksCache, sign_value,
                     verify_jwt, verify_signed_value, verify_webhook)
from .errors import MarketplaceError, not_configured
from .store import RESOURCE_ACTIVE, MarketplaceStore

MAX_NAME_LENGTH = 100
MAX_METADATA_BYTES = 4096
MAX_SCOPES = 64


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _text(value, limit=200):
    return str(value or "").strip()[:limit]


class MarketplaceService:
    def __init__(self, settings=None, store: Optional[MarketplaceStore] = None,
                 client: Optional[MarketplaceClient] = None, jwks: Optional[JwksCache] = None,
                 clock=time.time, credential_factory: Optional[Callable] = None,
                 session_secret: str = "", transport=None, redact=None,
                 resource_id_factory: Optional[Callable] = None):
        self.settings = settings or marketplace_config.load()
        self.clock = clock
        self.store = store
        self.client = client
        self.jwks = jwks
        self.redact = redact or build_redactor(self.settings.secrets())
        self.credential_factory = credential_factory
        self.session_secret = session_secret
        self.resource_id_factory = resource_id_factory or (
            lambda: "res_" + secrets.token_hex(8))

    # -- authentication ----------------------------------------------------
    def authenticate(self, authorization: Optional[str]) -> dict:
        """Verify a Partner API ``Authorization: Bearer <jwt>`` header.

        The one retry exists for rotation: a ``kid`` we have never seen is
        exactly the signal that Vercel published a new key, so the cache is
        refreshed once and the token re-checked. It is bounded to a single retry
        so a stream of forged tokens cannot turn into a stream of JWKS fetches.
        """
        if not self.settings.configured:
            raise not_configured(self.settings.missing())
        if not authorization or not authorization.lower().startswith("bearer "):
            raise MarketplaceError("ترويسة Authorization مفقودة أو غير مدعومة.",
                                   code="missing_authorization", status=401)
        token = authorization[7:].strip()
        if not token:
            raise MarketplaceError("التوكن فارغ.", code="missing_authorization", status=401)
        if self.jwks is None:
            raise MarketplaceError("خدمة التحقق من التوكن غير متاحة.",
                                   code="jwks_unavailable", status=503)
        try:
            return self._verify(token, force=False)
        except MarketplaceError as error:
            if error.code != "unknown_key":
                raise
        return self._verify(token, force=True)

    def _verify(self, token: str, force: bool) -> dict:
        keys = self.jwks.keys(force=force)
        return verify_jwt(token, keys, issuer=self.settings.issuer,
                          audience=self.settings.client_id, now=self.clock())

    @staticmethod
    def require_installation(claims: Mapping, installation_id: str) -> str:
        """The claim and the path must name the same installation.

        Neither check alone is enough: the token is proof of *a* customer, and
        the path is only a string from the URL. Comparing them is what stops a
        customer from reading another customer's resources by changing a path.
        """
        claimed = str((claims or {}).get("installation_id") or "")
        if not claimed or claimed != str(installation_id or ""):
            raise MarketplaceError("التوكن لا يطابق هذا التثبيت.",
                                   code="installation_mismatch", status=403)
        return claimed

    @staticmethod
    def require_admin(claims: Mapping):
        role = str((claims or {}).get("user_role") or "")
        if role and role != "ADMIN":
            raise MarketplaceError("هذه العملية تحتاج صلاحية ADMIN.",
                                   code="forbidden_role", status=403)
        return role or "ADMIN"

    # -- products and plans ------------------------------------------------
    def product_plans(self, product_id: str):
        if product_id != self.settings.product_id:
            raise MarketplaceError("المنتج غير معروف.", code="unknown_product", status=404)
        return {"plans": [dict(marketplace_config.FREE_PLAN)]}

    def installation_plans(self, installation_id: str):
        """Plans for one installation.

        Only the free plan ships, so the answer does not depend on the customer
        -- but the installation is still checked, because a plan list for a
        removed installation is a lie the dashboard would happily render.
        """
        installation = self._known_installation(installation_id)
        if installation is None:
            raise MarketplaceError("التثبيت غير معروف.", code="unknown_installation",
                                   status=404)
        return self.product_plans(self.settings.product_id)

    # -- installation lifecycle --------------------------------------------
    def _known_installation(self, installation_id):
        if self.store is None:
            return None
        return self.store.get_installation(installation_id)

    def upsert_installation(self, installation_id: str, payload: Mapping, claims: Mapping):
        """Create or refresh the installation Vercel just created.

        Vercel calls this during "Accept terms" and again on every scope change,
        so it is an upsert in the dictionary sense: idempotent, last-write-wins,
        and never an error just because the row is already there.
        """
        self.require_installation(claims, installation_id)
        if not isinstance(payload, Mapping):
            raise MarketplaceError("الطلب ليس كائناً JSON.", code="validation_error",
                                   status=400)
        credentials = payload.get("credentials")
        if not isinstance(credentials, Mapping):
            raise MarketplaceError("بيانات الاعتماد مفقودة.", code="validation_error",
                                   status=400,
                                   fields=[{"key": "credentials",
                                            "message": "credentials is required"}])
        access_token = _text(credentials.get("access_token"), 1024)
        if not access_token:
            raise MarketplaceError("رمز الوصول فارغ.", code="validation_error", status=400,
                                   fields=[{"key": "credentials.access_token",
                                            "message": "access_token is required"}])
        scopes = payload.get("scopes")
        scopes = [str(scope)[:120] for scope in scopes][:MAX_SCOPES] if isinstance(scopes, list) else []
        policies = payload.get("acceptedPolicies")
        policies = dict(policies) if isinstance(policies, Mapping) else {}
        account = payload.get("account") if isinstance(payload.get("account"), Mapping) else {}
        billing_plan_id = _text(payload.get("billingPlanId"), 100)
        if billing_plan_id and billing_plan_id != marketplace_config.FREE_PLAN["id"]:
            raise MarketplaceError("خطة الفوترة غير معروفة.", code="unknown_plan", status=400)
        if self.store is None:
            raise MarketplaceError("قاعدة البيانات غير متاحة.", code="store_unavailable",
                                   status=503)
        self.store.upsert_installation(
            installation_id, access_token=access_token,
            token_type=_text(credentials.get("token_type"), 40) or "Bearer",
            scopes=scopes, accepted_policies=policies, account=account,
            team_id=_text((claims or {}).get("account_id"), 120),
            billing_plan_id=billing_plan_id or marketplace_config.FREE_PLAN["id"])
        return {}

    # -- resources ---------------------------------------------------------
    def _validate_metadata(self, metadata):
        if metadata is None:
            return {}
        if not isinstance(metadata, Mapping):
            raise MarketplaceError("metadata يجب أن يكون كائناً.", code="validation_error",
                                   status=400,
                                   fields=[{"key": "metadata", "message": "must be an object"}])
        schema = marketplace_config.METADATA_SCHEMA["properties"]
        unknown = sorted(set(metadata) - set(schema))
        if unknown:
            raise MarketplaceError("حقول metadata غير معروفة.", code="validation_error",
                                   status=400,
                                   fields=[{"key": key, "message": "unknown field"}
                                           for key in unknown])
        clean = {}
        name = metadata.get("workspace_name")
        if name is not None:
            name = _text(name, 40)
            if not 3 <= len(name) <= 40:
                raise MarketplaceError("طول اسم مساحة العمل غير مقبول.",
                                       code="validation_error", status=400,
                                       fields=[{"key": "workspace_name",
                                                "message": "must be 3-40 characters"}])
            clean["workspace_name"] = name
        focus = metadata.get("focus")
        if focus is not None:
            focus = _text(focus, 40)
            if focus not in marketplace_config.FOCUS_CHOICES:
                raise MarketplaceError("قيمة المجال غير مقبولة.", code="validation_error",
                                       status=400,
                                       fields=[{"key": "focus",
                                                "message": "unsupported choice"}])
            clean["focus"] = focus
        if len(json.dumps(clean, ensure_ascii=False)) > MAX_METADATA_BYTES:
            raise MarketplaceError("metadata كبير جداً.", code="validation_error", status=400)
        return clean

    def _secrets_for(self, row: Mapping):
        """The env vars Vercel injects into whatever projects connect this."""
        return [{"name": key, "value": value}
                for key, value in self.settings.injected_env(
                    row.get("workspace_id") or "", row.get("api_token") or "").items()
                if value]

    def provision_resource(self, installation_id: str, payload: Mapping, claims: Mapping):
        """Create one workspace and hand Vercel its credentials.

        The secrets are generated *here*, not at read time, because the
        provisioning response is the only moment Vercel will store them; from
        then on the customer's projects read them as environment variables and
        nothing in this repository is asked for the value again.
        """
        self.require_installation(claims, installation_id)
        self.require_admin(claims)
        if self.store is None:
            raise MarketplaceError("قاعدة البيانات غير متاحة.", code="store_unavailable",
                                   status=503)
        if self.store.get_installation(installation_id) is None:
            raise MarketplaceError("التثبيت غير معروف.", code="unknown_installation",
                                   status=404)
        if not isinstance(payload, Mapping):
            raise MarketplaceError("الطلب ليس كائناً JSON.", code="validation_error",
                                   status=400)
        product_id = _text(payload.get("productId"), 100)
        if product_id != self.settings.product_id:
            raise MarketplaceError("المنتج غير معروف.", code="unknown_product", status=400,
                                   fields=[{"key": "productId", "message": "unknown product"}])
        plan_id = _text(payload.get("billingPlanId"), 100)
        if plan_id != marketplace_config.FREE_PLAN["id"]:
            raise MarketplaceError("خطة الفوترة غير معروفة.", code="unknown_plan", status=400,
                                   fields=[{"key": "billingPlanId",
                                            "message": "unknown plan"}])
        name = _text(payload.get("name"), MAX_NAME_LENGTH)
        if not name:
            name = "مساحة واحة"
        metadata = self._validate_metadata(payload.get("metadata"))
        if self.store.count_resources(installation_id) >= self.settings.max_resources:
            raise MarketplaceError("بلغ هذا التثبيت الحد الأقصى للموارد.",
                                   code="resource_limit_reached", status=409)
        if self.credential_factory is None:
            raise MarketplaceError("إصدار الرموز غير مُهيّأ على الخادم.",
                                   code="not_configured", status=503)
        workspace_id, api_token = self.credential_factory()
        resource_id = self.resource_id_factory()
        resource = self.store.create_resource(
            resource_id, installation_id, product_id, name, metadata, plan_id,
            workspace_id, self.settings.base_url, api_token, status="ready")
        row = self.store.raw_resource(installation_id, resource_id)
        self._announce(installation_id, resource_id, resource.get("status") or "ready")
        return {**resource, "secrets": self._secrets_for(row)}

    def list_resources(self, installation_id: str, claims: Mapping, ids=None):
        self.require_installation(claims, installation_id)
        if self.store is None:
            return {"resources": []}
        return {"resources": self.store.list_resources(installation_id, ids)}

    def get_resource(self, installation_id: str, resource_id: str, claims: Mapping):
        self.require_installation(claims, installation_id)
        if self.store is None:
            raise MarketplaceError("قاعدة البيانات غير متاحة.", code="store_unavailable",
                                   status=503)
        resource = self.store.get_resource(installation_id, resource_id)
        if resource is None:
            raise MarketplaceError("المورد غير موجود.", code="not_found", status=404)
        return resource

    def update_resource(self, installation_id: str, resource_id: str, payload: Mapping,
                        claims: Mapping):
        self.require_installation(claims, installation_id)
        self.require_admin(claims)
        if self.store is None:
            raise MarketplaceError("قاعدة البيانات غير متاحة.", code="store_unavailable",
                                   status=503)
        if not isinstance(payload, Mapping):
            raise MarketplaceError("الطلب ليس كائناً JSON.", code="validation_error",
                                   status=400)
        plan_id = payload.get("billingPlanId")
        if plan_id is not None and _text(plan_id, 100) != marketplace_config.FREE_PLAN["id"]:
            raise MarketplaceError("خطة الفوترة غير معروفة.", code="unknown_plan", status=400)
        metadata = (self._validate_metadata(payload.get("metadata"))
                    if "metadata" in payload else None)
        status = payload.get("status")
        if status is not None and _text(status, 40) not in RESOURCE_ACTIVE:
            raise MarketplaceError("حالة المورد غير مدعومة.", code="validation_error",
                                   status=400)
        updated = self.store.update_resource(
            installation_id, resource_id,
            name=_text(payload["name"], MAX_NAME_LENGTH) if payload.get("name") else None,
            metadata=metadata, status=_text(status, 40) if status is not None else None,
            billing_plan_id=_text(plan_id, 100) if plan_id is not None else None)
        if updated is None:
            raise MarketplaceError("المورد غير موجود.", code="not_found", status=404)
        return updated

    def delete_resource(self, installation_id: str, resource_id: str, claims: Mapping):
        self.require_installation(claims, installation_id)
        self.require_admin(claims)
        if self.store is None:
            raise MarketplaceError("قاعدة البيانات غير متاحة.", code="store_unavailable",
                                   status=503)
        if not self.store.delete_resource(installation_id, resource_id):
            raise MarketplaceError("المورد غير موجود.", code="not_found", status=404)
        return None

    def rotate_secrets(self, installation_id: str, resource_id: str, claims: Mapping):
        """Mint a new API token and push it to Vercel.

        Two halves, and the order matters: commit locally first, then push. If
        the push fails the local state is already consistent, and answering 202
        tells Vercel the rest is coming -- whereas pushing first would leave
        Vercel holding a token our database had not accepted yet.

        The old token stops working immediately, which is the point: rotation is
        what makes a leaked environment variable a bounded accident.
        """
        self.require_installation(claims, installation_id)
        self.require_admin(claims)
        if self.store is None:
            raise MarketplaceError("قاعدة البيانات غير متاحة.", code="store_unavailable",
                                   status=503)
        row = self.store.raw_resource(installation_id, resource_id)
        if row is None:
            raise MarketplaceError("المورد غير موجود.", code="not_found", status=404)
        if self.credential_factory is None:
            raise MarketplaceError("إصدار الرموز غير مُهيّأ على الخادم.",
                                   code="not_configured", status=503)
        workspace_id, api_token = self.credential_factory(
            workspace_id=row.get("workspace_id") or None)
        self.store.rotate_resource_token(installation_id, resource_id, api_token)
        secrets = self._secrets_for(self.store.raw_resource(installation_id, resource_id))
        pushed = self._push_secrets(installation_id, resource_id, secrets)
        if not pushed:
            # Async by contract: Vercel keeps the old values until we call
            # updateSecrets, which the dashboard will do on the next sync.
            return {"sync": False, "partial": True}, 202
        return {"sync": True, "secrets": secrets, "partial": False}, 200

    def _push_secrets(self, installation_id, resource_id, secrets):
        """Best effort. Returns False instead of raising on an upstream failure."""
        if self.client is None or self.store is None:
            return False
        installation = self.store.raw_installation(installation_id)
        if not installation or not installation.get("access_token"):
            return False
        try:
            return bool(self.client.update_secrets(installation["access_token"],
                                                   installation_id, resource_id, secrets))
        except MarketplaceError:
            return False

    def _announce(self, installation_id, resource_id, status):
        """Tell Vercel a resource changed. Never fatal to the caller."""
        if self.client is None or self.store is None:
            return False
        installation = self.store.raw_installation(installation_id)
        if not installation or not installation.get("access_token"):
            return False
        try:
            return self.client.dispatch_event(
                installation["access_token"], installation_id,
                {"type": "resource.updated", "productId": self.settings.product_id,
                 "resourceId": resource_id})
        except MarketplaceError:
            return False

    # -- webhooks ----------------------------------------------------------
    def handle_webhook(self, body: bytes, signature: Optional[str]):
        """Consume one Vercel webhook. Always returns; never raises.

        The ``200`` in every branch is not politeness. Vercel retries until it
        sees a 2xx, so raising here would replay an uninstall, and an exception
        raised while parsing one bad event would block every event after it.
        Failures are recorded in ``mp_events.handled`` -- where an operator can
        see them -- instead of being returned to Vercel as a retry request.
        """
        if not self.settings.configured:
            return {"accepted": False, "code": "not_configured"}, 200
        if not verify_webhook(body, signature, self.settings.client_secret):
            raise MarketplaceError("توقيع الويب هوك غير صالح.", code="invalid_signature",
                                   status=403)
        try:
            payload = json.loads((body or b"{}").decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {"accepted": False, "code": "invalid_json"}, 200
        if not isinstance(payload, dict):
            return {"accepted": False, "code": "invalid_payload"}, 200
        event_id = _text(payload.get("id"), 200)
        event_type = _text(payload.get("type"), 120)
        created_at = _text(payload.get("createdAt"), 60)
        inner = payload.get("payload") if isinstance(payload.get("payload"), Mapping) else {}
        installation_id = _text(inner.get("configuration", {}).get("id")
                                if isinstance(inner.get("configuration"), Mapping) else "",
                                200)
        if self.store is None:
            return {"accepted": False, "code": "store_unavailable"}, 200
        fresh = self.store.record_event(event_id or f"anon-{int(self.clock() * 1000)}",
                                        event_type or "unknown", created_at=created_at,
                                        installation_id=installation_id, payload=inner)
        outcome = {"accepted": True, "event_id": event_id, "type": event_type,
                   "duplicate": not fresh, "handled": "recorded"}
        if not fresh:
            return outcome, 200
        if event_type == "integration-configuration.removed" and installation_id:
            self.store.mark_uninstalled(installation_id)
            self.store.mark_event_handled(event_id, "uninstalled")
            outcome["handled"] = "uninstalled"
        else:
            self.store.mark_event_handled(event_id, "recorded")
        return outcome, 200

    # -- browser flows: install and SSO ------------------------------------
    def begin_install(self, params: Mapping, redirect_uri: Optional[str] = None):
        """Handle the Redirect URL: exchange the install code, then log in.

        Vercel opens this in a popup during installation. The exchange is the
        only place the OAuth code can be used -- it is single-use and valid for
        thirty minutes, so a code that reaches us twice is either a replay or a
        retry, and both are answered by trying once and reporting the failure.
        """
        if not self.settings.configured:
            raise not_configured(self.settings.missing())
        code = _text(params.get("code"), 512)
        if not code:
            raise MarketplaceError("رمز التثبيت مفقود.", code="missing_code", status=400)
        if self.client is None:
            raise MarketplaceError("عميل Vercel غير متاح.", code="client_unavailable",
                                   status=503)
        exchanged = self.client.exchange_oauth_code(code, redirect_uri)
        return {"access_token": exchanged["access_token"],
                "token_type": exchanged["token_type"],
                "installation_id": exchanged["installation_id"],
                "configuration_id": _text(params.get("configurationId"), 200)
                or exchanged.get("configuration_id") or "",
                "team_id": exchanged["team_id"] or _text(params.get("teamId"), 200),
                "next": _text(params.get("next"), 512),
                "source": _text(params.get("source"), 64)}

    def complete_install(self, exchanged: Mapping):
        """Persist what the install exchange produced.

        Marketplace installations are normally created by Vercel's server-side
        ``PUT /v1/installations/{id}``; when the browser flow runs first there is
        no such call yet, so the token is stored against the configuration id
        and the later upsert overwrites it with the authoritative record. Storing
        it under the configuration id -- not inventing an installation id -- is
        what keeps the two paths from creating two rows for one customer.
        """
        if self.store is None:
            return None
        installation_id = exchanged.get("installation_id") or exchanged.get("configuration_id")
        if not installation_id:
            return None
        if not exchanged.get("access_token"):
            return None
        self.store.upsert_installation(
            installation_id, access_token=exchanged["access_token"],
            token_type=exchanged.get("token_type") or "Bearer",
            team_id=exchanged.get("team_id") or "",
            billing_plan_id=marketplace_config.FREE_PLAN["id"])
        return self.store.get_installation(installation_id)

    def sso_login(self, params: Mapping):
        """Handle the Redirect Login URL: exchange a code, verify, return claims.

        The exchanged token is verified *before* it is believed. Exchanging a
        code with Vercel proves the code was real; only the signature proves the
        claims came from Vercel's marketplace key, and only the claims name the
        installation this session may see.
        """
        if not self.settings.configured:
            raise not_configured(self.settings.missing())
        code = _text(params.get("code"), 512)
        if not code:
            raise MarketplaceError("رمز الدخول مفقود.", code="missing_code", status=400)
        if self.client is None or self.jwks is None:
            raise MarketplaceError("خدمة الدخول غير متاحة.", code="client_unavailable",
                                   status=503)
        id_token = self.client.exchange_sso_token(code, _text(params.get("state"), 512))
        try:
            claims = self._verify(id_token, force=False)
        except MarketplaceError as error:
            if error.code != "unknown_key":
                raise
            claims = self._verify(id_token, force=True)
        return {"claims": claims,
                "resource_id": _text(params.get("resource_id"), 200),
                "project_id": _text(params.get("project_id"), 200)}

    def session_token(self, installation_id: str) -> str:
        """A short-lived signed handle for the dashboard cookie."""
        return sign_value(SESSION_PREFIX, installation_id, self.session_secret,
                          clock=self.clock)

    def read_session(self, token: Optional[str], installation_id: str) -> bool:
        return verify_signed_value(SESSION_PREFIX, installation_id, token,
                                   self.session_secret, self.settings.session_ttl_seconds,
                                   clock=self.clock)

    def state_token(self, value: str) -> str:
        return sign_value(STATE_PREFIX, value, self.session_secret, clock=self.clock)

    def read_state(self, token: Optional[str], value: str) -> bool:
        return verify_signed_value(STATE_PREFIX, value, token, self.session_secret,
                                   self.settings.state_ttl_seconds, clock=self.clock)

    # -- dashboard ---------------------------------------------------------
    def dashboard(self, installation_id: Optional[str]):
        """What the Arabic dashboard renders. No tokens, only fingerprints."""
        if self.store is None:
            return {"installations": [], "events": [], "configured": self.settings.configured}
        installations = ([] if not installation_id
                         else [item for item in self.store.list_installations(200)
                               if item["id"] == installation_id])
        if not installations:
            installations = self.store.list_installations(20)
        for installation in installations:
            installation["resources"] = self.store.list_resources(installation["id"])
        return {"installations": installations, "events": self.store.list_events(20),
                "configured": self.settings.configured}

    # -- status ------------------------------------------------------------
    def status(self):
        """What the owner console and the deploy doctor read. No secrets."""
        counts = {"installations": 0, "resources": 0, "events": 0}
        if self.store is not None:
            try:
                counts["installations"] = len(self.store.list_installations(200))
                counts["resources"] = self.store.count_all_resources()
                counts["events"] = len(self.store.list_events(200))
            except Exception:  # pragma: no cover - a broken DB must not hide status
                # Status is a report, not a probe: a database that will not
                # answer is itself the finding, and it is already visible in
                # /readyz. Swallowing it here keeps the page readable.
                pass
        return {"checked_at": _now_iso(), **self.settings.describe(),
                "counts": counts,
                "plans": [plan["id"] for plan in (marketplace_config.FREE_PLAN,)],
                "client_id_fingerprint": fingerprint(self.settings.client_id),
                "capabilities": {"install": self.settings.configured,
                                 "provision": self.settings.configured
                                 and self.settings.base_url_configured,
                                 "rotate": self.settings.configured,
                                 "webhook": self.settings.configured,
                                 "billing": False}}
