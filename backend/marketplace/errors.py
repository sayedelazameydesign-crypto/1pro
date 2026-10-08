"""Errors shaped the way the Vercel Marketplace expects them.

Vercel does not read our ``{"error": "...", "code": "..."}`` envelope on the
Partner API -- it reads ``{"error": {"code", "message", "fields", "user"}}`` and
renders ``user.message`` in the dashboard. Getting that shape wrong does not
raise anywhere: the store simply fails to create and the customer sees a
spinner. So the shape lives in one class and no route builds it by hand.

The ``code`` is also the contract the test suite asserts on. This repository
learned the hard way that a test matching a sentence in Arabic breaks whenever
the copy improves, so every refusal carries a machine-readable code and the
Arabic is free to change around it.
"""
from __future__ import annotations

from typing import Iterable, Mapping, Optional, Sequence


class MarketplaceError(Exception):
    """A refusal the Partner API turns into a JSON error instead of a 500 page.

    ``status`` is what Vercel is told; ``code`` is the identifier a test and a
    support thread can both quote; ``fields`` is Vercel's per-input validation
    detail (``[{"key": "region", "message": "..."}]``), which is how the create
    store modal highlights the offending box.
    """

    def __init__(self, message, code="marketplace_error", status=400,
                 fields: Optional[Sequence[Mapping[str, str]]] = None,
                 user_message: Optional[str] = None,
                 retry_after: Optional[int] = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.fields = tuple(fields or ())
        self.user_message = user_message
        self.retry_after = retry_after

    def describe(self) -> dict:
        """The Vercel error envelope. ``message`` stays server-side prose."""
        error: dict = {"code": self.code, "message": self.message}
        if self.user_message:
            error["user"] = {"message": self.user_message}
        if self.fields:
            error["fields"] = [dict(field) for field in self.fields]
        return {"error": error}

    def __repr__(self):
        return f"MarketplaceError(code={self.code!r}, status={self.status})"


def not_configured(missing: Iterable[str]) -> MarketplaceError:
    """The server has no credentials, so nothing here can be answered truthfully.

    503 rather than 401: the request was never authenticated *or* rejected, and
    telling Vercel "unauthorized" would make it retry a call that cannot
    succeed until an operator sets a variable.
    """
    names = ", ".join(sorted(str(name) for name in missing)) or "(none)"
    return MarketplaceError(f"تكامل Vercel Marketplace غير مُهيّأ: {names}",
                            code="not_configured", status=503)


# The one code that means "this came from Vercel's network, not from our logic".
UPSTREAM_CODES = frozenset({"upstream_unreachable", "upstream_timeout", "dns_failed",
                            "pre_http_network_failure", "vercel_unavailable",
                            "vercel_unauthorized"})
