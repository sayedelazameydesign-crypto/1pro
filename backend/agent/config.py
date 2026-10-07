"""Agent runtime configuration.

Everything is read from the environment once, at import, and validated so a
misconfigured deploy fails loudly at boot instead of mid-task. Values are
intentionally small: the target is Render's free instance (0.1 CPU, 512 MB)
plus a free Gemini quota shared by every visitor.
"""
import os


def _int(name, default, minimum, maximum):
    raw = os.environ.get(name, "")
    if not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as error:
        raise RuntimeError(f"{name} must be an integer, got {raw!r}") from error
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}, got {value}")
    return value


def _bool(name, default):
    raw = os.environ.get(name, "").strip().lower()
    if raw in ("", "auto"):
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise RuntimeError(f"{name} must be 1/0 or true/false, got {raw!r}")


class AgentConfig:
    """Runtime knobs. Public, secret-free; safe to expose via the API."""

    # Loop budgets.
    MAX_STEPS = _int("AGENT_MAX_STEPS", 5, 1, 10)
    MAX_TOOL_CALLS = _int("AGENT_MAX_TOOL_CALLS", 8, 1, 20)
    MAX_AI_CALLS = _int("AGENT_MAX_AI_CALLS", 8, 1, 20)
    DEADLINE_SECONDS = _int("AGENT_TASK_DEADLINE_SECONDS", 180, 30, 420)
    PROVIDER_TIMEOUT_SECONDS = _int("AGENT_PROVIDER_TIMEOUT_SECONDS", 75, 5, 75)

    # Generated output caps. The provider answer is capped too, so a chatty
    # model cannot blow the 512 MB box or the shared Gemini quota.
    MAX_GOAL_CHARS = _int("AGENT_MAX_GOAL_CHARS", 4000, 200, 8000)
    MAX_REPLY_CHARS = _int("AGENT_MAX_REPLY_CHARS", 8000, 1000, 24000)
    MAX_TOOL_INPUT_CHARS = _int("AGENT_MAX_TOOL_INPUT_CHARS", 1500, 200, 4000)
    MAX_ARTIFACT_BYTES = _int("AGENT_MAX_ARTIFACT_BYTES", 120000, 2000, 300000)

    # Concurrency. One worker by default: Render free has 0.1 CPU and a shared
    # Gemini quota, so parallel agents would only make every user slower.
    WORKERS = _int("AGENT_WORKERS", 1, 1, 4)
    MAX_ACTIVE_PER_USER = _int("AGENT_MAX_ACTIVE_PER_USER", 1, 1, 3)

    # Approvals. A paused-for-approval task waits this long, then is cancelled
    # cleanly (never executed "by default").
    APPROVAL_TIMEOUT_SECONDS = _int("AGENT_APPROVAL_TIMEOUT_SECONDS", 600, 30, 3600)

    # Memory and events.
    MEMORY_MAX_ITEMS = _int("AGENT_MEMORY_MAX_ITEMS", 40, 5, 200)
    EVENT_RETENTION_SECONDS = _int("AGENT_EVENT_RETENTION_SECONDS", 86400, 3600, 2592000)

    # Optional capabilities. Outbound fetches are off unless the operator opts
    # in, and every fetch still needs a per-task human approval unless
    # AGENT_AUTO_APPROVE_READ_ONLY=1.
    NETWORK_TOOLS = _bool("AGENT_NETWORK_TOOLS", False)
    AUTO_APPROVE_READ_ONLY = _bool("AGENT_AUTO_APPROVE_READ_ONLY", False)

    # Sandboxed code execution. Off unless the operator opts in *and* the machine
    # can build a boundary; `sandbox.detect()` decides the second half at run
    # time and refuses rather than running unguarded. These are requested
    # ceilings -- the runner enforces them or refuses the call, so a value here
    # never becomes a limit that only looks like one.
    CODE_EXEC = _bool("AGENT_CODE_EXEC", False)
    CODE_EXEC_TIMEOUT_SECONDS = _int("AGENT_CODE_EXEC_TIMEOUT_SECONDS", 15, 1, 120)
    CODE_EXEC_MEMORY_MB = _int("AGENT_CODE_EXEC_MEMORY_MB", 256, 64, 2048)
    CODE_EXEC_CPU_SECONDS = _int("AGENT_CODE_EXEC_CPU_SECONDS", 10, 1, 120)
    CODE_EXEC_MAX_OUTPUT_BYTES = _int("AGENT_CODE_EXEC_MAX_OUTPUT_BYTES", 4000, 500, 40000)

    # Model override for the agent path only; empty means "use the chat model".
    MODEL = os.environ.get("AGENT_MODEL", "").strip()

    @classmethod
    def describe(cls):
        return {
            "max_steps": cls.MAX_STEPS,
            "max_tool_calls": cls.MAX_TOOL_CALLS,
            "max_ai_calls": cls.MAX_AI_CALLS,
            "deadline_seconds": cls.DEADLINE_SECONDS,
            "max_goal_chars": cls.MAX_GOAL_CHARS,
            "workers": cls.WORKERS,
            "max_active_per_user": cls.MAX_ACTIVE_PER_USER,
            "approval_timeout_seconds": cls.APPROVAL_TIMEOUT_SECONDS,
            "network_tools": cls.NETWORK_TOOLS,
            "auto_approve_read_only": cls.AUTO_APPROVE_READ_ONLY,
            "memory_max_items": cls.MEMORY_MAX_ITEMS,
            "code_exec": cls.CODE_EXEC,
            "code_exec_limits": {"timeout_seconds": cls.CODE_EXEC_TIMEOUT_SECONDS,
                                 "memory_mb": cls.CODE_EXEC_MEMORY_MB,
                                 "cpu_seconds": cls.CODE_EXEC_CPU_SECONDS,
                                 "max_output_bytes": cls.CODE_EXEC_MAX_OUTPUT_BYTES},
            "model": cls.MODEL or None,
            "artifact_max_bytes": cls.MAX_ARTIFACT_BYTES,
            # Never promised: the free tier is shared and can be throttled.
            "guaranteed_capacity": False,
        }
