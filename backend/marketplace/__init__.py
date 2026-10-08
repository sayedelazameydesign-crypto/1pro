"""Waha as a Vercel Marketplace provider.

This package is the *provider* side of a native Marketplace integration: Vercel
calls us to create installations and provision resources, we hand back
credentials that Vercel injects into the customer's projects as environment
variables, and Vercel notifies us by webhook when an installation is removed.

It is deliberately separate from ``integrations/``, which is the surface *we*
call (GitHub Actions, Vercel deployments) behind the owner's own session. The
two differ in who authenticates and what a mistake costs, so they share
conventions -- Flask-free modules, an injected transport, a frozen config, and a
redactor built from the secrets -- but no code.

Layout::

    config.py    environment -> frozen settings; never serialises a secret
    errors.py    MarketplaceError -> Vercel's {error: {code, message, fields}}
    crypto.py    JWKS fetch, RS256 verification, webhook HMAC, signed values
    store.py     SQL for installations / resources / events; token-free views
    client.py    outbound Vercel calls: OAuth, SSO, events, secret pushes
    service.py   MarketplaceService: the only object app.py talks to

The HTTP surface lives in ``app.py`` on purpose, next to every other route, so
that the Host allowlist, the CORS policy and the error envelope are applied by
one code path rather than re-decided per package.
"""
from __future__ import annotations

from .client import MarketplaceClient  # noqa: F401
from .config import (CONFIGURE_PATH, DASHBOARD_PATH, FREE_PLAN, METADATA_SCHEMA,  # noqa: F401
                     PARTNER_PREFIX, REDIRECT_LOGIN_PATH, WEBHOOK_PATH,
                     MarketplaceSettings, load)
from .crypto import (JwksCache, RsaPublicKey, verify_jwt, verify_webhook,  # noqa: F401
                     webhook_signature)
from .errors import MarketplaceError  # noqa: F401
from .service import MarketplaceService  # noqa: F401
from .store import MarketplaceStore  # noqa: F401
