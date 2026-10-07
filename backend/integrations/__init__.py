"""Owner-facing integrations: GitHub Actions and Vercel, behind owner auth.

The package mirrors the rule ARCHITECTURE.md states for ``agent/``: **it knows
neither Flask nor the network**. ``app.py`` injects the transport, the config and
the redactor, so the whole surface is testable with a fake transport and no
secrets, and a different vendor can enter through the same door.

Layout::

    config.py    environment -> frozen settings; never serialises a secret
    redact.py    scrubs every configured secret out of any outbound message
    http.py      Transport protocol + urllib implementation + host allowlist
    github.py    GitHubClient: list workflow runs, dispatch a workflow
    vercel.py    VercelClient: list deployments, fire a deploy hook
    service.py   IntegrationService: the only object app.py talks to
"""
from .config import IntegrationConfig, load  # noqa: F401
from .http import HttpResponse, IntegrationError, Transport, UrllibTransport  # noqa: F401
from .redact import REDACTED, build_redactor, fingerprint  # noqa: F401
from .service import IntegrationService  # noqa: F401
