"""R7.2 — the isolation boundary, and an honest account of what it enforces.

The rule this module exists to keep: **a timeout is not isolation.** A Python
process on the same host can read the filesystem, reach the network, and spend the
machine's memory whatever the parent's timeout says. So execution is not offered
here as "run a subprocess"; it is offered as a *boundary* whose guarantees are
declared, checked, and reported back with every result.

The boundary is built from primitives the kernel actually provides:

* **user + network namespace** (`unshare -rn`) -- the child gets a fresh network
  namespace with no interface to the outside, so an outbound connection fails at
  the syscall rather than at a promise. Proven by `tests/test_agent_sandbox.py`,
  which attempts a real connection and requires it to be refused.
* **`setrlimit` inside the child** -- address space, CPU seconds, file size and
  process count, applied by the child to itself before any user code is compiled,
  so the limits hold for everything that code spawns.
* **a fixed environment allowlist** -- the child receives four literals and the
  parent's environment is never consulted. This is stronger than "remove the
  secrets": a variable nobody passes cannot leak, and a new secret added to the
  deployment tomorrow is invisible here by construction.
* **process-group teardown** -- the child is started in its own session and the
  whole group is killed on timeout, so a program that forked a helper cannot
  leave it running behind the kill.

What the boundary does **not** provide is stated as plainly, because the one thing
a sandbox must never do is overstate itself: the filesystem is shared with the
host. `filesystem_isolation` is therefore absent from `CAPABILITIES` below, every
result reports `filesystem_isolated: false`, and `code_exec`'s own description
says so where the model and the operator can both read it. A mount namespace that
hid the host tree is the next step, not a claim to make today.

Nothing here reads the deployment's environment or names a host: the boundary is
described by what it can do, and the caller passes in what it needs. That is also
what `tests/test_agent_no_platform_branching.py` enforces for every module in this
package.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass

# The strongest boundary available here, and the weakest. Reported verbatim in
# every result so a run is never ambiguous about which one produced it.
ISOLATION_NAMESPACE = "user+network-namespace+rlimits"
ISOLATION_NONE = "none"

# What a runner can claim. Each name is a guarantee a test can attempt to falsify
# -- `no_network` is asserted by trying to connect, not by reading this set.
CAPABILITY_NO_NETWORK = "no_network"
CAPABILITY_ENV_ALLOWLIST = "env_allowlist"
CAPABILITY_TIMEOUT = "timeout"
CAPABILITY_OUTPUT_CAP = "output_cap"
CAPABILITY_MEMORY_CAP = "memory_cap"
CAPABILITY_CPU_CAP = "cpu_cap"
CAPABILITY_PROCESS_CAP = "process_cap"
CAPABILITY_FILE_SIZE_CAP = "file_size_cap"
CAPABILITY_FILESYSTEM_ISOLATION = "filesystem_isolation"

CAPABILITIES = frozenset({
    CAPABILITY_NO_NETWORK, CAPABILITY_ENV_ALLOWLIST, CAPABILITY_TIMEOUT,
    CAPABILITY_OUTPUT_CAP, CAPABILITY_MEMORY_CAP, CAPABILITY_CPU_CAP,
    CAPABILITY_PROCESS_CAP, CAPABILITY_FILE_SIZE_CAP,
    CAPABILITY_FILESYSTEM_ISOLATION,
})

# What execution refuses to happen without. Deliberately not "everything in
# CAPABILITIES": a runner that cannot hide the host filesystem still stops
# exfiltration and resource exhaustion, and refusing that combination would mean
# offering nothing at all. The omissions are reported instead of assumed away --
# `filesystem_isolation` is the one guarantee deliberately not required, and each
# result carries its absence explicitly.
REQUIRED_CAPABILITIES = frozenset({
    CAPABILITY_NO_NETWORK, CAPABILITY_ENV_ALLOWLIST, CAPABILITY_TIMEOUT,
    CAPABILITY_OUTPUT_CAP, CAPABILITY_MEMORY_CAP, CAPABILITY_CPU_CAP,
})

# The child's entire environment. Four literals, no inheritance: the parent's
# environment is never read, so nothing has to be scrubbed and a secret added to
# the deployment later cannot appear here. HOME and TMPDIR are added per run and
# point inside the run's own directory, which keeps casual writes contained --
# hygiene, not isolation, and described as such.
CHILD_ENV = {
    "PATH": "/usr/bin:/bin",
    "LANG": "C.UTF-8",
    "PYTHONIOENCODING": "utf-8",
    "PYTHONDONTWRITEBYTECODE": "1",
}

# Runs inside the boundary, before any of the caller's code is compiled. The
# limits are applied by the child to itself, which is what makes them hold for
# everything the code goes on to spawn -- a parent-side `preexec_fn` would be
# unsafe here besides, because this package runs its tasks on worker threads.
BOOTSTRAP = (
    "import resource,sys\n"
    "mem,cpu,fsize,nproc=(int(a) for a in sys.argv[1:5])\n"
    "resource.setrlimit(resource.RLIMIT_AS,(mem<<20,mem<<20))\n"
    "resource.setrlimit(resource.RLIMIT_CPU,(cpu,cpu))\n"
    "resource.setrlimit(resource.RLIMIT_FSIZE,(fsize<<20,fsize<<20))\n"
    "resource.setrlimit(resource.RLIMIT_NPROC,(nproc,nproc))\n"
    "resource.setrlimit(resource.RLIMIT_CORE,(0,0))\n"
    "src=sys.stdin.read()\n"
    "try:\n"
    "    exec(compile(src,'<code_exec>','exec'),{'__name__':'__main__'})\n"
    "except SystemExit:\n"
    "    raise\n"
    "except BaseException:\n"
    "    import traceback;traceback.print_exc();sys.exit(1)\n"
)

DEFAULT_MAX_PROCESSES = 12
# A per-run ceiling on file writes, expressed in MiB. Not the output cap: it
# bounds what a runaway program can put on the shared disk, and it is set well
# above the output cap so a chatty program is truncated by the reader rather than
# killed by the kernel.
DEFAULT_FILE_SIZE_MB = 8


class SandboxRefused(RuntimeError):
    """Execution did not start. Carries a machine-readable reason.

    Raised *before* anything runs, which is the whole point: a boundary that
    cannot be made, or input that exceeds its cap, is a refusal and never a
    best-effort execution.
    """

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Limits:
    """Ceilings requested for one run. The runner enforces them or refuses."""

    timeout_seconds: int
    memory_mb: int
    cpu_seconds: int
    max_output_bytes: int
    max_input_bytes: int
    max_processes: int = DEFAULT_MAX_PROCESSES
    file_size_mb: int = DEFAULT_FILE_SIZE_MB


@dataclass(frozen=True)
class RunRequest:
    code: str
    limits: Limits


@dataclass(frozen=True)
class RunOutcome:
    """What happened. Never an exception for the program's own failure."""

    exit_status: int | None
    stdout: str
    stderr: str
    truncated: bool
    timed_out: bool
    duration_ms: int
    isolation: str
    filesystem_isolated: bool
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_status == 0 and not self.timed_out and not self.error

    def as_observation(self):
        """The shape the agent loop records and the store persists.

        `exit_status` travels with the output because the runtime writes this
        dict into `agent_tool_calls`; a result the store cannot tell apart from a
        success is how a failed program reads as a successful one.
        """
        return {
            "exit_status": self.exit_status,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "truncated": self.truncated,
            "timed_out": self.timed_out,
            "duration_ms": self.duration_ms,
            "isolation": self.isolation,
            "filesystem_isolated": self.filesystem_isolated,
            "error": self.error,
        }


class SandboxRunner:
    """A boundary. Subclasses declare what they enforce and then enforce it."""

    name = "unnamed"
    isolation = ISOLATION_NONE
    capabilities = frozenset()
    filesystem_isolated = False

    def missing(self, required=REQUIRED_CAPABILITIES):
        return sorted(set(required) - set(self.capabilities))

    def supports(self, required=REQUIRED_CAPABILITIES):
        return not self.missing(required)

    def run(self, request: RunRequest) -> RunOutcome:
        raise NotImplementedError


class UnavailableRunner(SandboxRunner):
    """The default. It refuses, and says exactly which guarantee is missing.

    A deployment with no way to build a boundary gets a tool that explains itself
    rather than one that quietly runs code unguarded. That asymmetry is the
    design: the failure mode of a missing sandbox must be "no execution", never
    "execution without the sandbox".
    """

    name = "unavailable"
    isolation = ISOLATION_NONE
    capabilities = frozenset()

    def __init__(self, reason):
        self.reason = reason

    def run(self, request: RunRequest) -> RunOutcome:
        raise SandboxRefused("sandbox_unavailable", self.reason)


class NamespaceRunner(SandboxRunner):
    """`unshare -rn` plus per-child `setrlimit`.

    The namespace is what makes `no_network` real rather than promised, and the
    rlimits are what make memory, CPU and file size real. The filesystem stays
    shared, and `filesystem_isolated = False` is reported on every result instead
    of being left for the reader to assume.
    """

    name = "unshare-netns"
    isolation = ISOLATION_NAMESPACE
    capabilities = REQUIRED_CAPABILITIES | {CAPABILITY_PROCESS_CAP,
                                            CAPABILITY_FILE_SIZE_CAP}
    filesystem_isolated = False

    def __init__(self, unshare_path, python_path=None):
        self.unshare = unshare_path
        self.python = python_path or sys.executable

    def _argv(self, limits):
        return [self.unshare, "-rn", "--", self.python, "-I", "-c", BOOTSTRAP,
                str(limits.memory_mb), str(limits.cpu_seconds),
                str(limits.file_size_mb), str(limits.max_processes)]

    def run(self, request: RunRequest) -> RunOutcome:
        limits = request.limits
        missing = self.missing()
        if missing:
            raise SandboxRefused(
                "boundary_missing",
                "this runner cannot enforce: " + ", ".join(missing))
        source = request.code or ""
        encoded = source.encode("utf-8")
        if len(encoded) > limits.max_input_bytes:
            raise SandboxRefused(
                "input_too_large",
                f"the program is {len(encoded)} bytes, over the "
                f"{limits.max_input_bytes}-byte limit")
        if not source.strip():
            raise SandboxRefused("empty_program", "there is no program to run")

        workdir = tempfile.mkdtemp(prefix="waha-code-exec-")
        started = time.time()
        timed_out = False
        try:
            outcome = self._spawn(request, workdir, started)
            timed_out = outcome.timed_out
            return outcome
        except subprocess.TimeoutExpired:
            # Reached only if the child outlived the wait; the group kill happens
            # in `_spawn`, which owns the process handle.
            timed_out = True
            raise
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
            _ = timed_out  # kept for readability of the control flow above

    def _spawn(self, request, workdir, started):
        limits = request.limits
        env = dict(CHILD_ENV)
        env["HOME"] = workdir
        env["TMPDIR"] = workdir
        # Output goes to unlinked temporary files rather than pipes: a pipe that
        # nobody drains while the child is being awaited is how a parent
        # deadlocks, or buffers a runaway program's output in its own memory.
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            process = subprocess.Popen(
                self._argv(limits), stdin=subprocess.PIPE, stdout=out, stderr=err,
                cwd=workdir, env=env, start_new_session=True, close_fds=True)
            try:
                process.stdin.write(request.code.encode("utf-8"))
                process.stdin.close()
            except (BrokenPipeError, OSError):
                # The child died before reading its program; the exit status and
                # stderr below are the real answer, so this is not an error here.
                pass
            except ValueError:
                pass
            timed_out = False
            try:
                process.wait(timeout=limits.timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_group(process)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            stdout, out_truncated = _read_capped(out, limits.max_output_bytes)
            stderr, err_truncated = _read_capped(err, limits.max_output_bytes)
        duration_ms = int((time.time() - started) * 1000)
        return RunOutcome(
            exit_status=process.returncode,
            stdout=stdout, stderr=stderr,
            truncated=bool(out_truncated or err_truncated),
            timed_out=timed_out, duration_ms=duration_ms,
            isolation=self.isolation,
            filesystem_isolated=self.filesystem_isolated,
            error=("timed out after %ss" % limits.timeout_seconds) if timed_out else "",
        )


def _kill_group(process):
    """Kill the child *and everything it started*.

    `process.kill()` alone leaves grandchildren running: a program that spawned a
    helper would outlive its timeout. The child was started in its own session,
    so one signal to the group reaches all of them.
    """
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.kill()
        except OSError:
            pass


def _read_capped(handle, cap):
    """Read at most `cap` bytes, and report whether more was written."""
    handle.seek(0, os.SEEK_END)
    size = handle.tell()
    handle.seek(0)
    raw = handle.read(cap)
    return raw.decode("utf-8", "replace"), size > cap


_probe_cache = {}


def probe_unshare(which=shutil.which, runner=subprocess.run):
    """Does this machine let an unprivileged process build the boundary?

    Asked by *doing* it -- the command is run and its exit status read -- because
    the answer depends on kernel policy (unprivileged user namespaces are
    restricted on some distributions) and not on whether a binary exists.
    """
    path = which("unshare")
    if not path:
        return None, "no `unshare` on this machine, so no network or user namespace"
    try:
        result = runner([path, "-rn", "--", "true"], capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as error:
        return None, f"`unshare -rn` could not be run: {type(error).__name__}"
    if result.returncode != 0:
        detail = (result.stderr or b"").decode("utf-8", "replace").strip()[:200]
        return None, (f"`unshare -rn` was refused by this kernel"
                      + (f": {detail}" if detail else ""))
    return path, ""


def detect(which=shutil.which, runner=subprocess.run, use_cache=True):
    """The strongest runner this machine can honestly offer.

    Cached: the probe forks a process, and the answer cannot change while the
    service is running. Tests pass `use_cache=False` so a monkeypatched probe is
    actually consulted.
    """
    if use_cache and "runner" in _probe_cache:
        return _probe_cache["runner"]
    path, reason = probe_unshare(which=which, runner=runner)
    decided = NamespaceRunner(path) if path else UnavailableRunner(reason)
    if use_cache:
        _probe_cache["runner"] = decided
    return decided


def reset_cache():
    """Forget the probe. Used by tests; harmless in production."""
    _probe_cache.clear()
