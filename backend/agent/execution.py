"""R6: the execution mode is a policy, not a branch inside the agent.

One agent, one provider interface, one tool registry, one store, one task semantic --
and exactly one difference between Render and Vercel: *where the loop runs*. On Render
a background worker owns the task (`queued`); on a platform that freezes the process the
moment the response is sent, or behind a gateway that only hands the visitor's token to
the request, the request itself must finish the work (`inline`).

That distinction has to live here, in a pure module, so it cannot leak:

    app.py  ->  resolve(...)  ->  ExecutionPolicy  ->  Service / Agent  ->  Agent.run()

`backend/agent/*` reads no platform variable and imports no Flask; `tests/
test_agent_no_platform_branching.py` fails if `if os.environ.get("VERCEL")`-style logic
creeps into the core. An "exception for one host" is how you end up with two products.

Budgets are the same public knobs (`AGENT_MAX_STEPS`, `AGENT_MAX_AI_CALLS`,
`AGENT_MAX_TOOL_CALLS`, `AGENT_TASK_DEADLINE_SECONDS`, `AGENT_PROVIDER_TIMEOUT_SECONDS`)
capped by the platform's request ceiling. There is deliberately **no**
`AGENT_SERVERLESS_*` variable: a knob earns its existence when an operator needs to
move it, and every operator need here is "shorter", which the existing knobs already
express. If a test ever shows that `inline` needs a number that `queued` cannot
describe, that test is the change request -- not this file.
"""

QUEUED = "queued"
INLINE = "inline"
MODES = (QUEUED, INLINE)

# Ceilings for a task that lives inside one HTTP request. Not new limits: they are caps,
# applied with min() against the public values, so `AGENT_MAX_STEPS=1` can only shorten
# an inline task further and `AGENT_MAX_STEPS=10` can never push one past the platform.
#
# * inline (PromptQL): the request can span two steps because the gateway has no hard
#   freeze, but it still cannot wait for a human, and 4 calls = plan + 2 tool turns +
#   report -- the last call must always be affordable for a report, or the visitor pays
#   for a task with no answer.
# * serverless: vercel.json declares maxDuration=60 on Hobby. The task deadline is 45s
#   so the terminal state is written *before* the platform kills the function (a task
#   frozen mid-run is the spinner this layer exists to prevent), and the provider call is
#   capped at 25s so one slow generation cannot eat the window. That leaves room for
#   exactly one step: plan+report on a single call, and no tool turn that can be billed
#   and then truncated.
INLINE_CAPS = {"MAX_STEPS": 2, "MAX_AI_CALLS": 4}
SERVERLESS_CAPS = {"MAX_STEPS": 1, "MAX_AI_CALLS": 3, "PROVIDER_TIMEOUT_SECONDS": 25,
                   "DEADLINE_SECONDS": 45}

# Which knobs a caller may read as a budget. Kept in one tuple so `/api/agent/config`
# advertises exactly what the loop enforces, not a parallel copy of it.
BUDGET_FIELDS = ("MAX_STEPS", "MAX_AI_CALLS", "MAX_TOOL_CALLS", "DEADLINE_SECONDS",
                 "PROVIDER_TIMEOUT_SECONDS")


class ExecutionPolicyError(ValueError):
    """The policy was asked for something this contract does not express."""


class CappedConfig:
    """A config view with numeric knobs tightened, never loosened.

    The agent reads `deps.config` as usual, so a request-bound task needs no special
    casing anywhere in the loop: the caps arrive as the config itself.
    """

    def __init__(self, base, **overrides):
        if base is None:
            raise ExecutionPolicyError("a capped config needs a base config")
        object.__setattr__(self, "_base", base)
        applied = {}
        for name, value in overrides.items():
            current = getattr(base, name, None)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ExecutionPolicyError(f"cap {name}={value!r} must be a positive int")
            if current is not None and not isinstance(current, int):
                raise ExecutionPolicyError(f"base {name}={current!r} is not a number")
            # min(), not a raise: an operator who sets AGENT_MAX_STEPS=1 has already gone
            # below the platform cap and must not be punished for it. The cap can only
            # tighten, so no caller can use this layer to widen a budget -- a cap that
            # binds is simply not applied, and caps below reports what actually changed.
            if value < current:
                applied[name] = value
        object.__setattr__(self, "_caps", applied)
        self.__dict__.update(applied)

    def __getattr__(self, name):
        return getattr(self.__dict__["_base"], name)

    @property
    def caps(self):
        """{knob: (configured, enforced)} for every tightened value -- public and secret-free."""
        base = self.__dict__["_base"]
        return {name: [getattr(base, name), value]
                for name, value in sorted(self.__dict__["_caps"].items())}

    def describe(self):
        return {"max_steps": self.MAX_STEPS, "max_ai_calls": self.MAX_AI_CALLS,
                "max_tool_calls": self.MAX_TOOL_CALLS,
                "deadline_seconds": self.DEADLINE_SECONDS,
                "provider_timeout_seconds": self.PROVIDER_TIMEOUT_SECONDS,
                "caps_applied": self.caps}


def caps_for(mode, serverless=False):
    """The caps a mode implies. Pure function of two facts -- no platform sniffing here."""
    if mode not in MODES:
        raise ExecutionPolicyError(f"unknown execution mode {mode!r}; expected one of {MODES}")
    if mode == QUEUED:
        if serverless:
            raise ExecutionPolicyError(
                "a serverless request cannot own a queued task: nothing runs the queue "
                "after the response is sent")
        return {}
    return dict(INLINE_CAPS, **SERVERLESS_CAPS) if serverless else dict(INLINE_CAPS)


class ExecutionPolicy:
    """What the deployment means for *where* and *how long* a task runs.

    Deliberately inert: it holds no store, no token, no secret. It answers four
    questions -- is this request-bound, may a tool wait for a human, whose visitor
    token is valid, and what config does the loop get -- and nothing else.
    """

    __slots__ = ("mode", "serverless", "reasons", "base_config", "_config")

    def __init__(self, mode, *, serverless=False, config=None, reasons=()):
        self.mode = mode
        self.serverless = bool(serverless)
        self.reasons = tuple(reasons)
        # `base_config` stays the untouched public view; `_config` is derived from it.
        # Deriving from the base (never from an already-capped view) is what makes a cap
        # idempotent here and impossible to widen by accident.
        self.base_config = config
        self._config = self.apply(config)

    # -- the four answers -----------------------------------------------------
    @property
    def is_inline(self):
        return self.mode == INLINE

    @property
    def allows_approvals(self):
        """A task bound to a request cannot pause for a person. Refused, not deferred."""
        return self.mode == QUEUED

    @property
    def uses_request_visitor_token(self):
        """PromptQL mints a token that only the request carries; Vercel must not borrow it.

        Passing one on a serverless host would hand the loop a credential whose audience
        is a gateway that is not in the path -- wrong identity, not merely useless.
        """
        return self.is_inline and not self.serverless

    @property
    def config(self):
        """The config the loop gets: capped for inline, the public one for queued."""
        return self._config

    # -- descriptions ---------------------------------------------------------
    def apply(self, config):
        """Derive the loop's config from a base one. The caps only ever tighten it."""
        caps = caps_for(self.mode, serverless=self.serverless)
        if config is None:
            return None
        if caps:
            return CappedConfig(config, **caps)
        return config

    def describe(self):
        """Public, secret-free: what a client may rely on for this deployment."""
        return {"mode": self.mode,
                "approvals_allowed": self.allows_approvals,
                "uses_request_visitor_token": self.uses_request_visitor_token,
                "budget": _budget(self._config),
                "caps_applied": self._config.caps if isinstance(self._config, CappedConfig) else {},
                "reasons": list(self.reasons),
                "new_knobs": 0}

    def __eq__(self, other):
        if not isinstance(other, ExecutionPolicy):
            return NotImplemented
        return (self.mode, self.serverless, self.reasons) == \
               (other.mode, other.serverless, other.reasons)

    def __hash__(self):
        return hash((self.mode, self.serverless, self.reasons))

    def __repr__(self):
        return f"ExecutionPolicy(mode={self.mode!r}, serverless={self.serverless!r})"


def _budget(config):
    if config is None:
        return None
    return {name.lower(): getattr(config, name) for name in BUDGET_FIELDS}


def resolve(*, ai_mode="standalone", serverless=False, config=None):
    """Turn two deployment *facts* into a policy. This is the whole branching surface.

    The caller (app.py) is the only place that knows `VERCEL` exists; this function never
    reads an environment, which is what makes both modes testable in one process without
    mutating global state -- and what keeps `queued` and `inline` two schedules over the
    same semantics rather than two implementations.
    """
    serverless = bool(serverless)
    reasons = []
    if serverless:
        reasons.append("serverless: the instance is frozen once the response is sent, "
                       "so a queued task would never start")
    if ai_mode == "promptql":
        reasons.append("promptql: only the request carries the visitor's gateway token")
    mode = INLINE if reasons else QUEUED
    return ExecutionPolicy(mode, serverless=serverless, config=config, reasons=reasons)
