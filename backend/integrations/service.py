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
from .drive import DriveClient
from .github import GitHubClient
from .http import Transport, UrllibTransport
from .redact import build_redactor, fingerprint
from .render import RenderClient
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
        self.render = RenderClient(self.config.render, self.redact,
                                   transport=self.transport,
                                   timeout=self.config.timeout_seconds,
                                   resolver=self.resolver)
        # No resolver here: Drive is a fixed API host, not an operator-supplied URL,
        # so the rebinding guard that the two deploy hooks need does not apply.
        self.drive = DriveClient(self.config.drive, self.redact,
                                 transport=self.transport,
                                 timeout=self.config.timeout_seconds,
                                 clock=clock)

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
            "render": {
                **cfg.describe()["render"],
                "token_fingerprint": fingerprint(cfg.render.api_key),
                "capabilities": {"list_deploys": cfg.render.configured,
                                 "trigger_hook": cfg.render.hook_configured},
            },
            "drive": {
                **cfg.describe()["drive"],
                # The refresh triple is the durable credential, so the fingerprint
                # names it when present; an operator on the short-lived trial path
                # gets the access token's fingerprint instead.
                "token_fingerprint": (fingerprint(cfg.drive.refresh_token)
                                      or fingerprint(cfg.drive.access_token)),
                "capabilities": {"list_files": cfg.drive.configured,
                                 "upload": cfg.drive.configured,
                                 "default_folder": cfg.drive.folder_configured},
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

    # -- Render ------------------------------------------------------------
    def render_deploys(self, limit=None):
        result = self.render.list_deploys(limit or self.config.page_size)
        result["fetched_at"] = _now()
        return result

    def render_deploy(self):
        result = self.render.trigger_deploy_hook()
        result["at"] = _now()
        return result

    # -- Google Drive ------------------------------------------------------
    def drive_files(self, limit=None, folder_id=None):
        result = self.drive.list_files(limit or self.config.page_size, folder_id=folder_id)
        result["fetched_at"] = _now()
        return result

    def drive_upload(self, name, text, mime_type="text/markdown", folder_id=None):
        result = self.drive.upload_text(name, text, mime_type=mime_type,
                                        folder_id=folder_id)
        result["at"] = _now()
        return result
