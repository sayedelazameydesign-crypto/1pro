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

import os
from dataclasses import dataclass
from typing import FrozenSet, Mapping, Tuple

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
}

GITHUB_API_BASE = "https://api.github.com"
VERCEL_API_BASE = "https://api.vercel.com"


def _int(raw, default, minimum, maximum):
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def _origins(raw):
    return frozenset(origin.strip().rstrip("/")
                     for origin in str(raw or "").split(",") if origin.strip())


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


@dataclass(frozen=True)
class IntegrationConfig:
    owner: OwnerSettings
    github: GitHubSettings
    vercel: VercelSettings
    timeout_seconds: int = API_TIMEOUT_SECONDS
    page_size: int = DEFAULT_PAGE_SIZE

    def secrets(self) -> Tuple[str, ...]:
        """Every value that must never appear in a response, log or stack trace.

        The deploy hook URL is included because Vercel puts a secret path segment
        in it -- leaking the URL is leaking the ability to deploy.
        """
        return (self.owner.token, self.github.token, self.vercel.token,
                self.vercel.deploy_hook)

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
        timeout_seconds=_int(env.get("WAHA_INTEGRATIONS_TIMEOUT"),
                             API_TIMEOUT_SECONDS, 1, 120),
        page_size=_int(env.get("WAHA_INTEGRATIONS_PAGE_SIZE"),
                       DEFAULT_PAGE_SIZE, 1, MAX_PAGE_SIZE),
    )
