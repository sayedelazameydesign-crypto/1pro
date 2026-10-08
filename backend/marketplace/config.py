"""Operator settings and the product catalogue for the Vercel Marketplace surface.

Flask-free and network-free on purpose, with the same rule ``integrations/config.py``
follows: it reads the environment and describes *whether* something is
configured, never the value. ``secrets()`` feeds the redactor so a Vercel error
body echoed back by a route cannot carry our client secret with it.

The difference from the owner integrations is who is on the other end. Those are
called by us with a token we hold; this surface is called *by Vercel*, so the
only thing standing between an unauthenticated POST and a provisioned resource
is ``client_id``/``client_secret`` being present and every check in
``crypto.py`` running. An unset secret therefore has to be a loud 503, never a
permissive default.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Tuple
from urllib.parse import urlsplit

MARKETPLACE_ISSUER = "https://marketplace.vercel.com"
DEFAULT_JWKS_URL = "https://marketplace.vercel.com/.well-known/jwks"
DEFAULT_API_BASE = "https://api.vercel.com"
DEFAULT_PRODUCT_ID = "waha-workspace"
DEFAULT_ENV_PREFIX = "WAHA"
DEFAULT_TIMEOUT_SECONDS = 15
DEFAULT_SESSION_TTL_SECONDS = 8 * 3600
DEFAULT_STATE_TTL_SECONDS = 15 * 60
DEFAULT_MAX_RESOURCES = 5

# Which of our own endpoints Vercel (or the customer's browser) is told about.
# Kept here, and not written into the routes, so the operator console and the
# repository docs cannot drift from what the server actually serves.
PARTNER_PREFIX = "/v1"
WEBHOOK_PATH = "/v1/webhooks/vercel"
CONFIGURE_PATH = "/marketplace/configure"
REDIRECT_LOGIN_PATH = "/marketplace/callback"
DASHBOARD_PATH = "/marketplace"

# The one billing plan this integration ships. It is free and says so:
# ``paymentMethodRequired`` false is what lets Vercel create a store without a
# card, and claiming a paid plan here would mean implementing the invoice and
# billing-data endpoints, which this repository has not done. See
# CAPABILITY-MATRIX.md, where marketplace billing is recorded as Missing rather
# than Mocked.
FREE_PLAN = {
    "id": "waha-free",
    "type": "subscription",
    "name": "مجاني",
    "description": "مساحة عمل واحة واحدة ورمز واجهة برمجية واحد، يُحقن في مشروعك على Vercel.",
    "scope": "resource",
    "paymentMethodRequired": False,
    "cost": "$0.00/month",
    "highlightedDetails": [
        {"label": "مساحات عمل", "value": "1 لكل مورد"},
        {"label": "السعر", "value": "$0.00/شهر"},
    ],
    "details": [
        {"label": "حقن متغيرات البيئة في Production وPreview وDevelopment"},
        {"label": "تدوير الرمز من لوحة المورد"},
        {"label": "واجهة عربية RTL"},
    ],
    "requiredPolicies": [],
}

# What the "Create store" modal asks for. Plain JSON Schema: Vercel renders it
# directly, and the same object is what the server validates `metadata` against
# (see service._validate_metadata), so the form and the check cannot disagree.
METADATA_SCHEMA = {
    "type": "object",
    "properties": {
        "workspace_name": {
            "type": "string",
            "title": "اسم مساحة العمل",
            "minLength": 3,
            "maxLength": 40,
        },
        "focus": {
            "type": "string",
            "title": "المجال",
            "enum": ["general", "learning", "assistant"],
            "default": "general",
        },
    },
    "required": ["workspace_name"],
    "additionalProperties": False,
}

FOCUS_CHOICES = ("general", "learning", "assistant")


def _int(raw, default, minimum, maximum):
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def _clean_url(raw: str) -> str:
    """Keep a configured URL as scheme://host[/path] and drop anything else.

    A trailing slash here becomes a double slash in every Partner API path we
    hand Vercel, so it is normalised once, at the boundary.
    """
    value = str(raw or "").strip()
    if not value:
        return ""
    if not value.startswith(("http://", "https://")):
        return ""
    return value.rstrip("/")


@dataclass(frozen=True)
class MarketplaceSettings:
    client_id: str
    client_secret: str
    slug: str
    product_id: str
    base_url: str
    redirect_url: str
    api_base: str = DEFAULT_API_BASE
    jwks_url: str = DEFAULT_JWKS_URL
    issuer: str = MARKETPLACE_ISSUER
    env_prefix: str = DEFAULT_ENV_PREFIX
    session_ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS
    state_ttl_seconds: int = DEFAULT_STATE_TTL_SECONDS
    max_resources: int = DEFAULT_MAX_RESOURCES
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS

    @property
    def configured(self) -> bool:
        """Both halves of the OAuth pair, or nothing works.

        A client id alone is worse than neither: the code exchange would fail
        with a 403 from Vercel that reads like the customer did something wrong.
        """
        return bool(self.client_id and self.client_secret)

    def missing(self) -> Tuple[str, ...]:
        out = []
        if not self.client_id:
            out.append("VERCEL_INTEGRATION_CLIENT_ID")
        if not self.client_secret:
            out.append("VERCEL_INTEGRATION_CLIENT_SECRET")
        return tuple(out)

    @property
    def base_url_configured(self) -> bool:
        return bool(self.base_url)

    def secrets(self) -> Tuple[str, ...]:
        """Everything that must never reach a response, a log line or a trace."""
        return (self.client_secret, self.client_id)

    def env_keys(self) -> Tuple[str, ...]:
        """The variable names injected into the customer's Vercel project.

        Prefixed and fixed so a customer can grep for them, and so a future
        second product cannot collide with the first.
        """
        return (f"{self.env_prefix}_API_BASE", f"{self.env_prefix}_API_TOKEN",
                f"{self.env_prefix}_WORKSPACE_ID")

    def injected_env(self, workspace_id: str, api_token: str) -> dict:
        """Name -> value for the three injected variables."""
        base, token, workspace = self.env_keys()
        return {base: self.base_url or "", token: api_token, workspace: workspace_id}

    def describe(self) -> dict:
        """The public shape: booleans, paths and variable *names* only."""
        return {
            "configured": self.configured,
            "slug": self.slug or None,
            "product_id": self.product_id,
            "base_url_configured": self.base_url_configured,
            "redirect_url_configured": bool(self.redirect_url),
            "missing": list(self.missing()),
            "paths": {"partner_prefix": PARTNER_PREFIX, "webhook": WEBHOOK_PATH,
                      "configure": CONFIGURE_PATH, "redirect_login": REDIRECT_LOGIN_PATH,
                      "dashboard": DASHBOARD_PATH},
            "env_keys": list(self.env_keys()),
            "limits": {"session_ttl_seconds": self.session_ttl_seconds,
                       "state_ttl_seconds": self.state_ttl_seconds,
                       "max_resources": self.max_resources,
                       "timeout_seconds": self.timeout_seconds},
        }


def load(environ: Mapping[str, str] | None = None) -> MarketplaceSettings:
    """Read the environment once. Callers keep the result; nothing re-reads env."""
    env = os.environ if environ is None else environ
    get = lambda key, default="": str(env.get(key, default) or "").strip()  # noqa: E731
    return MarketplaceSettings(
        client_id=get("VERCEL_INTEGRATION_CLIENT_ID"),
        client_secret=get("VERCEL_INTEGRATION_CLIENT_SECRET"),
        slug=get("VERCEL_INTEGRATION_SLUG"),
        product_id=get("WAHA_MARKETPLACE_PRODUCT_ID", DEFAULT_PRODUCT_ID) or DEFAULT_PRODUCT_ID,
        base_url=_clean_url(get("WAHA_MARKETPLACE_BASE_URL")),
        redirect_url=_clean_url(get("WAHA_MARKETPLACE_REDIRECT_URL")),
        api_base=_clean_url(get("WAHA_MARKETPLACE_API_BASE")) or DEFAULT_API_BASE,
        jwks_url=_clean_url(get("WAHA_MARKETPLACE_JWKS_URL")) or DEFAULT_JWKS_URL,
        issuer=get("WAHA_MARKETPLACE_ISSUER", MARKETPLACE_ISSUER) or MARKETPLACE_ISSUER,
        env_prefix=(get("WAHA_MARKETPLACE_ENV_PREFIX", DEFAULT_ENV_PREFIX).upper()
                    or DEFAULT_ENV_PREFIX),
        session_ttl_seconds=_int(env.get("WAHA_MARKETPLACE_SESSION_TTL"),
                                 DEFAULT_SESSION_TTL_SECONDS, 300, 24 * 3600),
        state_ttl_seconds=_int(env.get("WAHA_MARKETPLACE_STATE_TTL"),
                               DEFAULT_STATE_TTL_SECONDS, 60, 3600),
        max_resources=_int(env.get("WAHA_MARKETPLACE_MAX_RESOURCES"),
                           DEFAULT_MAX_RESOURCES, 1, 100),
        timeout_seconds=_int(env.get("WAHA_MARKETPLACE_TIMEOUT"),
                             DEFAULT_TIMEOUT_SECONDS, 1, 60),
    )


def same_host(left: str, right: str) -> bool:
    """True when two configured URLs name the same host. Used by the doctor.

    Vercel compares the ``redirect_uri`` in the code exchange to the Redirect
    URL in its console *exactly*. A mismatch is a 403 that mentions neither
    side's value, so the doctor compares them here instead.
    """
    try:
        return bool(urlsplit(left).netloc) and urlsplit(left).netloc == urlsplit(right).netloc
    except ValueError:
        return False
