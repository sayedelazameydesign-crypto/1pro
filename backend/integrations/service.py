"""The only object ``app.py`` talks to.

Everything above this line is vendor-specific and Flask-free; this module owns
the three cross-cutting rules of the owner surface so that no route can forget
one of them:

1. **Redaction.** The redactor is built from ``config.secrets()`` here and handed
   to both clients, so an upstream error body cannot leak a token no matter which
   route forwarded it.
2. **Honest readiness.** ``status()`` reports what is configured *now*, from the
   same config object the calls will use. It never says "ready" for an
   integration whose variable is missing -- the same rule the components panel
   follows ("an unread state is written unknown, never coloured").
3. **Confirmation is checked at the edge, not here.** ``app.py`` refuses a
   mutation whose body lacks the action's phrase; this service only performs it.
   Keeping the check in the route layer is what makes it testable through the
   HTTP surface, where a client would actually hit it.
"""
from __future__ import annotations

import datetime
import time

from .config import IntegrationConfig, load
from .github import GitHubClient
from .http import Transport, UrllibTransport
from .redact import build_redactor, fingerprint
from .vercel import VercelClient


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


class IntegrationService:
    def __init__(self, config: IntegrationConfig | None = None, transport: Transport = None,
                 clock=time.time, resolver=None):
        self.config = config or load()
        self.transport = transport or UrllibTransport()
        self.clock = clock
        self.resolver = resolver
        self.redact = build_redactor(self.config.secrets())
        self.github = GitHubClient(self.config.github, self.redact,
                                   transport=self.transport,
                                   timeout=self.config.timeout_seconds)
        self.vercel = VercelClient(self.config.vercel, self.redact,
                                   transport=self.transport,
                                   timeout=self.config.timeout_seconds,
                                   resolver=self.resolver)

    # -- status ------------------------------------------------------------
    def status(self):
        """What is usable right now. Secrets are described by fingerprint only."""
        cfg = self.config
        return {
            "checked_at": _now(),
            "github": {
                **cfg.describe()["github"],
                "token_fingerprint": fingerprint(cfg.github.token),
                "capabilities": {"list_runs": cfg.github.configured,
                                 "dispatch": cfg.github.configured and bool(cfg.github.workflow)},
            },
            "vercel": {
                **cfg.describe()["vercel"],
                "token_fingerprint": fingerprint(cfg.vercel.token),
                "capabilities": {"list_deployments": cfg.vercel.configured,
                                 "trigger_hook": cfg.vercel.hook_configured},
            },
            "limits": cfg.describe()["owner"],
            "confirm_phrases": cfg.describe()["confirm_phrases"],
        }

    # -- GitHub ------------------------------------------------------------
    def github_runs(self, limit=None, branch=None):
        result = self.github.list_runs(limit or self.config.page_size, branch=branch)
        result["fetched_at"] = _now()
        return result

    def github_dispatch(self, ref="main", inputs=None):
        result = self.github.dispatch(ref=ref, inputs=inputs)
        result["at"] = _now()
        return result

    # -- Vercel ------------------------------------------------------------
    def vercel_deployments(self, limit=None):
        result = self.vercel.list_deployments(limit or self.config.page_size)
        result["fetched_at"] = _now()
        return result

    def vercel_deploy(self):
        result = self.vercel.trigger_deploy_hook()
        result["at"] = _now()
        return result
