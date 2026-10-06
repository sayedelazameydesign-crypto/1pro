"""Waha Agent Runtime.

A small, provider-agnostic agent core that turns the single-shot chat backend
into a plan -> act -> observe -> report loop with persisted task state, tool
calls behind explicit approval, user memory and generated artifacts (a minimal
"canvas").

Layout (kept deliberately flat so the free Render tier stays easy to reason about):

    config.py     env-driven knobs, validated at import time
    providers.py  Provider interface + Gemini (direct / PromptQL) + fake
    tools.py      Tool registry and the sandboxed built-in tools
    store.py      SQL persistence (SQLite + Postgres) for tasks/steps/events
    runtime.py    the loop itself: plan, act, observe, reflect, report
    service.py    task queue, approvals, cancellation, SSE feed

Hard rules inherited from the MVP: no secrets in code, no shell, no filesystem
writes, no code execution, no silent provider fallback. See ARCHITECTURE.md.
"""
