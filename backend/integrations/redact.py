"""Scrub configured secrets out of anything that leaves the process.

Upstream APIs are not careful about echoing what you sent them. GitHub answers
``401 {"message": "Bad credentials"}`` today, but a ``422`` on a malformed
dispatch has been known to echo the request back, and a Vercel error body can
contain the ``projectId`` and query string you posted. Since the whole point of
this surface is that ``GITHUB_*`` / ``VERCEL_*`` / ``DEPLOY_HOOK_*`` stay on the
server, "stay on the server" has to include *the error text we forward to the
browser*, not just the happy path.

So every message that reaches a response or a log line goes through a redactor
built from ``IntegrationConfig.secrets()``.
"""
from __future__ import annotations

import hashlib
from typing import Callable, Iterable

REDACTED = "[REDACTED]"
# Below this length a "secret" is more likely to be a common substring than a
# credential, and replacing it would mangle ordinary words in an error message.
MIN_SECRET_LENGTH = 8


def build_redactor(values: Iterable[str]) -> Callable[[object], str]:
    """Return ``redact(text) -> str`` with every known secret replaced.

    Longest first, so a hook URL that embeds a token is removed whole instead of
    leaving its surrounding path behind.
    """
    needles = sorted({str(value) for value in values
                      if value and len(str(value)) >= MIN_SECRET_LENGTH},
                     key=len, reverse=True)

    def redact(text: object) -> str:
        out = "" if text is None else str(text)
        for needle in needles:
            if needle in out:
                out = out.replace(needle, REDACTED)
        return out

    return redact


def identity_redactor() -> Callable[[object], str]:
    """A redactor for tests that deliberately have no secrets to protect."""
    return lambda text: "" if text is None else str(text)


def fingerprint(value: str) -> str:
    """A stable, non-reversible handle for a credential, safe to display.

    Used so the UI can show *which* token is in use ("ghp_…3f9a") after a rotation
    without the server ever sending the value. Two characters of the digest are
    shown: enough to tell two tokens apart, far too little to search for.
    """
    if not value:
        return ""
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"{digest[:4]}…{digest[-4:]}"
