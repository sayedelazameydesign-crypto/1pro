"""Operator credentials and limits for the ``/integrations`` admin surface.

Flask-free on purpose, and it holds no network code: it reads the environment and
describes *whether* something is configured, never the value. The rule is the one
ARCHITECTURE.md already applies to the visitor path -- no key ever reaches the
browser -- so:

* ``describe()`` emits booleans and the *names* of missing variables only;
* ``secrets()`` hands the raw values to ``redact.build_redactor`` so that any
  message leaving the process (HTTP error text, log line, JSON response) has them
  scrubbed;
* nothing here writes to disk, because the serverless filesystem is read-only and
  a credential on a temporary disk is a credential that outlives its rotation.
"""
from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass
from typing import FrozenSet, Mapping, Tuple
from urllib.parse import urlsplit

# An owner session is deliberately shorter-lived than a visitor token (400 days).
# This surface can trigger a production deployment; 8 hours is one working day.
OWNER_SESSION_TTL_SECONDS = 8 * 3600
# Mutations are rare and destructive-ish (a real CI run, a real deploy), so the
# ceiling is a per-minute one, not the per-hour budget the visitor path uses.
OWNER_WRITE_LIMIT_PER_MINUTE = 3
# Brute-forcing WAHA_OWNER_TOKEN is the first thing an attacker would try, so the
# login endpoint gets its own, much tighter, per-IP counter.
OWNER_LOGIN_LIMIT_PER_HOUR = 5
API_TIMEOUT_SECONDS = 15
DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100

# A mutation only runs when the request repeats the action's phrase. This is not
# authentication -- the session already did that -- it is a guard against a
# replayed or half-built client firing a deploy it never meant to.
CONFIRM_PHRASES = {
    "github_dispatch": "dispatch-ci",
    "vercel_deploy": "deploy",
    # The two additions are writes for the same reason as the two above: one
    # redeploys a live service, the other creates a file in the owner's Drive.
    "render_deploy": "deploy-render",
    "drive_upload": "upload-drive",
}

GITHUB_API_BASE = "https://api.github.com"
VERCEL_API_BASE = "https://api.vercel.com"
RENDER_API_BASE = "https://api.render.com/v1"
GOOGLE_DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"
GOOGLE_DRIVE_UPLOAD_BASE = "https://www.googleapis.com/upload/drive/v3"
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"


def _int(raw, default, minimum, maximum):
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def normalize_hostname(raw):
    """Return one canonical exact hostname, or ``None`` for unsafe syntax.

    Host allowlists deliberately do not support wildcards, ports, URLs, or legacy
    numeric IPv4 spellings. A suffix wildcard would let a rebinding-controlled
    subdomain become trusted; a port is ignored by Werkzeug's host check anyway.
    """
    value = str(raw or "").strip()
    if not value or any(ord(char) < 0x21 for char in value):
        return None
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    if any(char in value for char in "/\\@?#*%"):
        return None
    value = value.rstrip(".")
    if not value:
        return None
    try:
        return ipaddress.ip_address(value).compressed.lower()
    except ValueError:
        pass
    # Browsers normalize shortened numeric IPv4 forms such as 127.1. Do not
    # accept those as DNS names in a security allowlist.
    if all(char in "0123456789." for char in value):
        return None
    if ":" in value:
        return None
    try:
        ascii_host = value.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None
    if len(ascii_host) > 253:
        return None
    labels = ascii_host.split(".")
    if any(not label or len(label) > 63
           or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label)
           for label in labels):
        return None
    return ascii_host


def normalize_origin(raw):
    """Canonicalize a browser Origin; reject paths, credentials and odd syntax."""
    value = str(raw or "").strip()
    if not value or "\\" in value or any(ord(char) < 0x20 for char in value):
        return None
    try:
        parts = urlsplit(value)
        scheme = parts.scheme.lower()
        raw_host = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if (scheme not in ("http", "https") or not parts.netloc or not raw_host
            or parts.username is not None or parts.password is not None
            or parts.path not in ("", "/") or parts.query or parts.fragment):
        return None
    host = normalize_hostname(raw_host)
    if host is None:
        return None
    default_port = 443 if scheme == "https" else 80
    authority_host = f"[{host}]" if ":" in host else host
    authority = authority_host if port in (None, default_port) else f"{authority_host}:{port}"
    return f"{scheme}://{authority}"


def parse_origins(raw):
    """Parse a comma-separated explicit origin allowlist into canonical origins."""
    return frozenset(normalized for item in str(raw or "").split(",")
                     if (normalized := normalize_origin(item)))


def configured_trusted_hosts(environ=None):
    """Exact service hosts from explicit config and hosting-platform metadata."""
    env = os.environ if environ is None else environ
    candidates = [item.strip() for item in str(env.get("WAHA_TRUSTED_HOSTS", "") or "").split(",")
                  if item.strip()]
    candidates.extend(str(env.get(key, "") or "").strip() for key in (
        "RENDER_EXTERNAL_HOSTNAME", "VERCEL_URL", "VERCEL_PROJECT_PRODUCTION_URL",
        "VERCEL_BRANCH_URL"))
    return tuple(sorted({host for candidate in candidates
                         if (host := normalize_hostname(candidate))}))


def trusted_hosts(environ=None):
    """Build Flask's host allowlist from exact origins, platform hosts and dev loopback.

    Platform variables cover the canonical Render and Vercel hostnames. A custom
    domain can be added with ``WAHA_TRUSTED_HOSTS``. In production, the deploy
    doctor requires one of those sources so a public Host header is never trusted
    merely because it equals an attacker-controlled Origin.
    """
    env = os.environ if environ is None else environ
    hosts = set(configured_trusted_hosts(env))
    for key in ("WAHA_ALLOWED_ORIGINS", "WAHA_OWNER_ALLOWED_ORIGINS"):
        for origin in parse_origins(env.get(key, "")):
            host = normalize_hostname(urlsplit(origin).hostname)
            if host:
                hosts.add(host)
    hosts.update(("localhost", "127.0.0.1"))
    return tuple(sorted(hosts))


def _origins(raw):
    """Backward-compatible private name for the config parser."""
    return parse_origins(raw)


@dataclass(frozen=True)
class GitHubSettings:
    token: str
    repo: str
    workflow: str
    api_base: str = GITHUB_API_BASE

    @property
    def configured(self) -> bool:
        return bool(self.token and self.repo)

    def missing(self) -> Tuple[str, ...]:
        out = []
        if not self.token:
            out.append("GITHUB_TOKEN")
        if not self.repo:
            out.append("GITHUB_REPO")
        return tuple(out)


@dataclass(frozen=True)
class VercelSettings:
    token: str
    project_id: str
    team_id: str
    deploy_hook: str
    api_base: str = VERCEL_API_BASE

    @property
    def configured(self) -> bool:
        return bool(self.token and self.project_id)

    @property
    def hook_configured(self) -> bool:
        return bool(self.deploy_hook)

    def missing(self) -> Tuple[str, ...]:
        out = []
        if not self.token:
            out.append("VERCEL_TOKEN")
        if not self.project_id:
            out.append("VERCEL_PROJECT_ID")
        if not self.deploy_hook:
            out.append("DEPLOY_HOOK_URL")
        return tuple(out)


@dataclass(frozen=True)
class RenderSettings:
    """Render's Blueprints API and its deploy hook, held apart on purpose.

    Mirrors the Vercel split: reading deploy history needs ``RENDER_API_KEY``,
    changing production needs only the hook URL. The service id is Render's own
    ``srv-…`` identifier, so a wrong one is a 404 rather than a deploy of
    something else.
    """
    api_key: str
    service_id: str
    deploy_hook: str
    api_base: str = RENDER_API_BASE

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.service_id)

    @property
    def hook_configured(self) -> bool:
        return bool(self.deploy_hook)

    def missing(self) -> Tuple[str, ...]:
        out = []
        if not self.api_key:
            out.append("RENDER_API_KEY")
        if not self.service_id:
            out.append("RENDER_SERVICE_ID")
        if not self.deploy_hook:
            out.append("RENDER_DEPLOY_HOOK_URL")
        return tuple(out)


@dataclass(frozen=True)
class DriveSettings:
    """Google Drive, through an OAuth grant the owner minted once.

    Two shapes are accepted because both are real in this project's lifecycle:

    * ``GOOGLE_DRIVE_ACCESS_TOKEN`` -- what ``gcloud`` or a one-off consent run
      produces. Short-lived, perfect for a trial, never stored by the server.
    * the refresh triple -- ``GOOGLE_DRIVE_REFRESH_TOKEN`` + client id + secret.
      The client mints a fresh access token per request, so a restart cannot
      strand the surface on an expired 60-minute token.

    ``refresh_token`` is a secret and is therefore in ``IntegrationConfig
    .secrets()``; Drive's own error bodies quote the ``access_token`` they were
    given, which is exactly the leak the redactor exists for.
    """
    access_token: str
    refresh_token: str
    client_id: str
    client_secret: str
    folder_id: str
    api_base: str = GOOGLE_DRIVE_API_BASE
    upload_base: str = GOOGLE_DRIVE_UPLOAD_BASE
    token_uri: str = GOOGLE_TOKEN_URI

    @property
    def configured(self) -> bool:
        return bool(self.access_token or (self.refresh_token and self.client_id
                                          and self.client_secret))

    @property
    def folder_configured(self) -> bool:
        return bool(self.folder_id)

    def missing(self) -> Tuple[str, ...]:
        if self.access_token:
            return ()
        if self.refresh_token and self.client_id and self.client_secret:
            return ()
        out = []
        if not (self.refresh_token and self.client_id and self.client_secret):
            out.append("GOOGLE_DRIVE_ACCESS_TOKEN (أو الثلاثي: "
                       "GOOGLE_DRIVE_REFRESH_TOKEN + GOOGLE_DRIVE_CLIENT_ID + "
                       "GOOGLE_DRIVE_CLIENT_SECRET)")
        return tuple(out)


# ``GOOGLE_DRIVE_FOLDER_ID`` is deliberately absent from ``DriveSettings.missing()``
# above: uploads without it land in "My Drive" root, which is a working outcome and
# not a missing configuration.
@dataclass(frozen=True)
class OwnerSettings:
    token: str
    session_ttl_seconds: int = OWNER_SESSION_TTL_SECONDS
    write_limit_per_minute: int = OWNER_WRITE_LIMIT_PER_MINUTE
    login_limit_per_hour: int = OWNER_LOGIN_LIMIT_PER_HOUR
    allowed_origins: FrozenSet[str] = frozenset()

    @property
    def configured(self) -> bool:
        return bool(self.token)

    def missing(self) -> Tuple[str, ...]:
        return () if self.token else ("WAHA_OWNER_TOKEN",)


# Defaults for a config built without the two newer providers: an omitted provider
# must read as "not configured" (503 from its routes) rather than as an absent
# attribute, so existing callers keep working while the portal gains cards.
UNCONFIGURED_RENDER = RenderSettings(api_key="", service_id="", deploy_hook="")
UNCONFIGURED_DRIVE = DriveSettings(access_token="", refresh_token="", client_id="",
                                   client_secret="", folder_id="")


@dataclass(frozen=True)
class IntegrationConfig:
    owner: OwnerSettings
    github: GitHubSettings
    vercel: VercelSettings
    render: RenderSettings = UNCONFIGURED_RENDER
    drive: DriveSettings = UNCONFIGURED_DRIVE
    timeout_seconds: int = API_TIMEOUT_SECONDS
    page_size: int = DEFAULT_PAGE_SIZE

    def secrets(self) -> Tuple[str, ...]:
        """Every value that must never appear in a response, log or stack trace.

        The deploy hook URL is included because Vercel puts a secret path segment
        in it -- leaking the URL is leaking the ability to deploy. Render's hook is
        the same shape, and Drive's refresh token outlives every access token the
        server mints from it, so it belongs here even though no response echoes it.
        """
        return (self.owner.token, self.github.token, self.vercel.token,
                self.vercel.deploy_hook, self.render.api_key, self.render.deploy_hook,
                self.drive.access_token, self.drive.refresh_token,
                self.drive.client_secret)

    def describe(self) -> dict:
        """The public shape: booleans, limits and variable *names* only."""
        return {
            "github": {"configured": self.github.configured,
                       "repo": self.github.repo or None,
                       "workflow": self.github.workflow or None,
                       "missing": list(self.github.missing())},
            "vercel": {"configured": self.vercel.configured,
                       "project_id": self.vercel.project_id or None,
                       "has_deploy_hook": self.vercel.hook_configured,
                       "missing": list(self.vercel.missing())},
            "render": {"configured": self.render.configured,
                       "service_id": self.render.service_id or None,
                       "has_deploy_hook": self.render.hook_configured,
                       "missing": list(self.render.missing())},
            "drive": {"configured": self.drive.configured,
                      # Which shape is in use, without saying what it is: an
                      # ephemeral access token expires on its own, a refresh token
                      # does not, and the operator should know which one is live.
                      "credential": ("access_token" if self.drive.access_token
                                     else "refresh_token" if self.drive.configured
                                     else None),
                      "folder_id": self.drive.folder_id or None,
                      "missing": list(self.drive.missing())},
            "owner": {"configured": self.owner.configured,
                      "session_ttl_seconds": self.owner.session_ttl_seconds,
                      "write_limit_per_minute": self.owner.write_limit_per_minute,
                      "login_limit_per_hour": self.owner.login_limit_per_hour},
            "confirm_phrases": dict(CONFIRM_PHRASES),
            "timeout_seconds": self.timeout_seconds,
        }


def load(environ: Mapping[str, str] | None = None) -> IntegrationConfig:
    """Read the environment once. Callers keep the result; nothing re-reads env."""
    env = os.environ if environ is None else environ
    get = lambda key, default="": str(env.get(key, default) or "").strip()  # noqa: E731
    return IntegrationConfig(
        owner=OwnerSettings(
            token=get("WAHA_OWNER_TOKEN"),
            session_ttl_seconds=_int(env.get("WAHA_OWNER_SESSION_TTL"),
                                     OWNER_SESSION_TTL_SECONDS, 300, 24 * 3600),
            write_limit_per_minute=_int(env.get("WAHA_OWNER_WRITE_LIMIT_PER_MINUTE"),
                                        OWNER_WRITE_LIMIT_PER_MINUTE, 1, 60),
            login_limit_per_hour=_int(env.get("WAHA_OWNER_LOGIN_LIMIT_PER_HOUR"),
                                      OWNER_LOGIN_LIMIT_PER_HOUR, 1, 100),
            allowed_origins=_origins(env.get("WAHA_OWNER_ALLOWED_ORIGINS")),
        ),
        github=GitHubSettings(
            token=get("GITHUB_TOKEN"),
            repo=get("GITHUB_REPO"),
            workflow=get("GITHUB_WORKFLOW_ID") or get("GITHUB_WORKFLOW"),
            api_base=get("GITHUB_API_BASE", GITHUB_API_BASE).rstrip("/"),
        ),
        vercel=VercelSettings(
            token=get("VERCEL_TOKEN"),
            project_id=get("VERCEL_PROJECT_ID"),
            team_id=get("VERCEL_TEAM_ID"),
            deploy_hook=get("DEPLOY_HOOK_URL"),
            api_base=get("VERCEL_API_BASE", VERCEL_API_BASE).rstrip("/"),
        ),
        render=RenderSettings(
            api_key=get("RENDER_API_KEY"),
            service_id=get("RENDER_SERVICE_ID"),
            deploy_hook=get("RENDER_DEPLOY_HOOK_URL"),
            api_base=get("RENDER_API_BASE", RENDER_API_BASE).rstrip("/"),
        ),
        drive=DriveSettings(
            access_token=get("GOOGLE_DRIVE_ACCESS_TOKEN"),
            refresh_token=get("GOOGLE_DRIVE_REFRESH_TOKEN"),
            client_id=get("GOOGLE_DRIVE_CLIENT_ID"),
            client_secret=get("GOOGLE_DRIVE_CLIENT_SECRET"),
            folder_id=get("GOOGLE_DRIVE_FOLDER_ID"),
            api_base=get("GOOGLE_DRIVE_API_BASE", GOOGLE_DRIVE_API_BASE).rstrip("/"),
            upload_base=get("GOOGLE_DRIVE_UPLOAD_BASE",
                            GOOGLE_DRIVE_UPLOAD_BASE).rstrip("/"),
            token_uri=get("GOOGLE_TOKEN_URI", GOOGLE_TOKEN_URI),
        ),
        timeout_seconds=_int(env.get("WAHA_INTEGRATIONS_TIMEOUT"),
                             API_TIMEOUT_SECONDS, 1, 120),
        page_size=_int(env.get("WAHA_INTEGRATIONS_PAGE_SIZE"),
                       DEFAULT_PAGE_SIZE, 1, MAX_PAGE_SIZE),
    )
