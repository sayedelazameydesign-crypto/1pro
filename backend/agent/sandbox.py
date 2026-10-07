"""R7.2/R7.3 — the isolation boundary, and an honest account of what it enforces.

The rule this module exists to keep: **a timeout is not isolation**, and neither is
a timeout plus resource limits. A Python process on the same host can read the
application's files, its database, its configuration, and write to any of them.
So execution here is not "run a subprocess"; it is a *boundary*, built from kernel
primitives, whose guarantees are declared, checked, and reported with every result.

What the boundary enforces, and how each is proven rather than asserted:

* **no network** -- a network namespace, so an outbound connection fails at the
  syscall. Proven by opening a socket to a real address and requiring failure.
* **an isolated filesystem** -- a mount namespace plus a private root: a tmpfs
  becomes `/`, the language runtime is bind-mounted into it *read-only*, and
  exactly two directories are writable (`/workspace`, `/tmp`). The host tree is
  not hidden behind a flag; it is absent, because it was never mounted into the
  new root. Proven by opening a host path for writing and then checking the host
  filesystem for the file.
* **a separate PID namespace** -- the program is PID 1 of its own tree, `/proc`
  shows only that tree, and the kernel reaps the whole namespace when it exits.
* **resource ceilings** -- address space, CPU seconds and file size are applied by
  the child to itself before any of the program is compiled, so they hold for
  everything it spawns.

What it does **not** enforce is stated as plainly, because the one thing a sandbox
must never do is overstate itself: there is no **aggregate** memory ceiling. The
per-process `RLIMIT_AS` multiplied by the process ceiling is the worst case, and
that product is computed and reported on every run (`aggregate_memory_bound_mb`)
instead of being left for the reader to work out. A true tree-wide limit needs
cgroup v2, and this module does not claim one -- `detect()` reports the boundary it
can actually build, and a host that cannot build it gets no execution at all.

The threat model is stated here so it is not implied: the boundary defends against
arbitrary code *reaching or modifying anything outside its workspace*. It is not a
defence against a kernel exploit, and it is not a claim that a process holding
`CAP_SYS_ADMIN` inside its own user namespace is harmless -- only that nothing
worth reaching is inside the root it can see.

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

# The boundary, as a name and as a claim. Reported verbatim in every result so a
# run is never ambiguous about what produced it.
ISOLATION_PRIVATE_ROOT = "user+mount+pid+network-namespace/private-root"
ISOLATION_NETWORK_ONLY = "user+network-namespace"
ISOLATION_NONE = "none"

# What a runner can claim. Each name is a guarantee a test can attempt to falsify.
CAPABILITY_NO_NETWORK = "no_network"
CAPABILITY_ENV_ALLOWLIST = "env_allowlist"
CAPABILITY_TIMEOUT = "timeout"
CAPABILITY_OUTPUT_CAP = "output_cap"
CAPABILITY_MEMORY_CAP = "memory_cap"
CAPABILITY_CPU_CAP = "cpu_cap"
CAPABILITY_PROCESS_CAP = "process_cap"
CAPABILITY_FILE_SIZE_CAP = "file_size_cap"
CAPABILITY_FILESYSTEM_ISOLATION = "filesystem_isolation"
CAPABILITY_PID_ISOLATION = "pid_isolation"

CAPABILITIES = frozenset({
    CAPABILITY_NO_NETWORK, CAPABILITY_ENV_ALLOWLIST, CAPABILITY_TIMEOUT,
    CAPABILITY_OUTPUT_CAP, CAPABILITY_MEMORY_CAP, CAPABILITY_CPU_CAP,
    CAPABILITY_PROCESS_CAP, CAPABILITY_FILE_SIZE_CAP,
    CAPABILITY_FILESYSTEM_ISOLATION, CAPABILITY_PID_ISOLATION,
})

# What execution refuses to happen without. `filesystem_isolation` is in here on
# purpose and is not configurable: the tool's description tells the model that the
# program cannot touch the application's files, and a boundary that sometimes
# keeps that promise is a boundary that lies. An operator who wants execution on a
# host that cannot build this root gets a clear refusal instead -- which is the
# only failure mode that is safe by construction.
REQUIRED_CAPABILITIES = frozenset({
    CAPABILITY_NO_NETWORK, CAPABILITY_ENV_ALLOWLIST, CAPABILITY_TIMEOUT,
    CAPABILITY_OUTPUT_CAP, CAPABILITY_MEMORY_CAP, CAPABILITY_CPU_CAP,
    CAPABILITY_PROCESS_CAP, CAPABILITY_FILE_SIZE_CAP,
    CAPABILITY_FILESYSTEM_ISOLATION, CAPABILITY_PID_ISOLATION,
})

# The child's entire environment. Four literals, no inheritance: the parent's
# environment is never read, so nothing has to be scrubbed and a secret added to
# the deployment later cannot appear here. HOME and TMPDIR are added per run and
# point inside the sandbox root.
CHILD_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "LANG": "C.UTF-8",
    "PYTHONIOENCODING": "utf-8",
    "PYTHONDONTWRITEBYTECODE": "1",
}

# Paths bind-mounted into the private root, read-only, when they exist. This is
# the allowlist: anything not named here is simply not in the new root.
RUNTIME_PATHS = ("/usr", "/lib", "/lib64", "/bin", "/sbin", "/etc/alternatives")

# Device nodes a program may reasonably expect. Bound read-write; a missing one is
# skipped, because a sandbox that fails to start over /dev/zero is worse than one
# without it.
DEVICE_PATHS = ("/dev/null", "/dev/zero", "/dev/urandom", "/dev/random")

DEFAULT_MAX_PROCESSES = 4
DEFAULT_FILE_SIZE_MB = 8

# Runs as the first and only process inside every namespace. It builds the root,
# then runs the program. Kept as one string so the boundary is one readable thing
# and no helper file has to exist for a sandbox to start.
#
# Order matters and is the whole trick: a tmpfs is mounted at the new root *first*,
# so every directory created afterwards lives in the new root rather than being
# created on the host and then hidden by it.
INIT = (
    "import ctypes,os,sys\n"
    "B,R,REM,REC,PRIV=4096,1,32,16384,1<<18\n"
    "L=ctypes.CDLL('libc.so.6',use_errno=True)\n"
    "def M(s,t,f=None,fl=0):\n"
    "    if L.mount(s and s.encode(),t.encode(),f and f.encode(),fl,None)!=0:\n"
    "        e=ctypes.get_errno();raise OSError(e,os.strerror(e),'%s -> %s'%(s,t))\n"
    "root,ws,mem,cpu,fsize,nproc=sys.argv[1:7]\n"
    "os.makedirs(root,exist_ok=True)\n"
    "M(None,'/',None,REC|PRIV)\n"
    "M('tmpfs',root,'tmpfs',0)\n"
    "for s in ['/usr','/lib','/lib64','/bin','/sbin','/etc/alternatives']+sys.argv[7:]:\n"
    "    if not s or not os.path.exists(s):continue\n"
    "    t=os.path.join(root,s.strip('/'));os.makedirs(t,exist_ok=True)\n"
    "    M(s,t,None,B|REC)\n"
    "    M(None,t,None,B|REM|R|REC)\n"
    "for n in ['tmp','workspace','proc','dev']:\n"
    "    os.makedirs(os.path.join(root,n),exist_ok=True)\n"
    "M(ws,os.path.join(root,'workspace'),None,B|REC)\n"
    "for d in ['/dev/null','/dev/zero','/dev/urandom','/dev/random']:\n"
    "    if os.path.exists(d):\n"
    "        t=os.path.join(root,d.strip('/'))\n"
    "        open(t,'a').close();M(d,t,None,B)\n"
    "os.chroot(root);os.chdir('/workspace')\n"
    "try:M('proc','/proc','proc')\n"
    "except OSError:pass\n"
    "import resource\n"
    "for r,v in [(resource.RLIMIT_AS,int(mem)<<20),(resource.RLIMIT_CPU,int(cpu)),\n"
    "            (resource.RLIMIT_FSIZE,int(fsize)<<20),(resource.RLIMIT_NPROC,int(nproc)),\n"
    "            (resource.RLIMIT_CORE,0)]:\n"
    "    resource.setrlimit(r,(v,v))\n"
    "src=sys.stdin.read()\n"
    "try:\n"
    "    exec(compile(src,'<code_exec>','exec'),{'__name__':'__main__'})\n"
    "except SystemExit:\n"
    "    raise\n"
    "except BaseException:\n"
    "    import traceback;traceback.print_exc();sys.exit(1)\n"
)


class SandboxRefused(RuntimeError):
    """Execution did not start. Carries a machine-readable reason.

    Raised *before* anything runs, which is the point: a boundary that cannot be
    made, or input that exceeds its cap, is a refusal and never a best-effort run.
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

    @property
    def aggregate_memory_bound_mb(self):
        """Worst case for the whole tree: per-process ceiling × process ceiling.

        Reported on every run because it is the honest answer to "how much memory
        can this use", and the honest answer is not "memory_mb". A tree-wide cap
        needs cgroup v2, which this boundary does not claim.
        """
        return self.memory_mb * self.max_processes


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
    aggregate_memory_bound_mb: int
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_status == 0 and not self.timed_out and not self.error

    def as_observation(self):
        """The shape the agent loop records and the store persists.

        `exit_status` travels with the output because the runtime writes this dict
        into `agent_tool_calls`; a result the store cannot tell apart from a
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
            "aggregate_memory_bound_mb": self.aggregate_memory_bound_mb,
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

    # -- shared plumbing ------------------------------------------------------
    def _refuse_if_incomplete(self):
        missing = self.missing()
        if missing:
            raise SandboxRefused("boundary_missing",
                                 "this runner cannot enforce: " + ", ".join(missing))

    @staticmethod
    def _refuse_if_unusable(request):
        source = request.code or ""
        encoded = source.encode("utf-8")
        if not source.strip():
            raise SandboxRefused("empty_program", "there is no program to run")
        if len(encoded) > request.limits.max_input_bytes:
            raise SandboxRefused(
                "input_too_large",
                f"the program is {len(encoded)} bytes, over the "
                f"{request.limits.max_input_bytes}-byte limit")

    def _finish(self, request, process, out, err, started, timed_out):
        limits = request.limits
        stdout, out_truncated = _read_capped(out, limits.max_output_bytes)
        stderr, err_truncated = _read_capped(err, limits.max_output_bytes)
        return RunOutcome(
            exit_status=process.returncode,
            stdout=stdout, stderr=stderr,
            truncated=bool(out_truncated or err_truncated),
            timed_out=timed_out,
            duration_ms=int((time.time() - started) * 1000),
            isolation=self.isolation,
            filesystem_isolated=self.filesystem_isolated,
            aggregate_memory_bound_mb=limits.aggregate_memory_bound_mb,
            error=("timed out after %ss" % limits.timeout_seconds) if timed_out else "",
        )


class UnavailableRunner(SandboxRunner):
    """The default. It refuses, and says which guarantee is missing.

    A host with no way to build a boundary gets a tool that explains itself rather
    than one that quietly runs code unguarded. That asymmetry is the design: the
    failure mode of a missing sandbox is "no execution", never "execution without
    the sandbox".
    """

    name = "unavailable"
    isolation = ISOLATION_NONE
    capabilities = frozenset()

    def __init__(self, reason):
        self.reason = reason

    def run(self, request: RunRequest) -> RunOutcome:
        raise SandboxRefused("sandbox_unavailable", self.reason)


class NamespaceRunner(SandboxRunner):
    """User + network namespace, per-child `setrlimit`. **No filesystem isolation.**

    Kept because it is a real boundary and because the refusal path needs a
    boundary that is genuinely incomplete to test against. `detect()` prefers the
    private root, and `REQUIRED_CAPABILITIES` excludes this runner, so it never
    executes anything through the tool: it is the honest answer to "what if only
    this much is available", which is "refuse".
    """

    name = "unshare-netns"
    isolation = ISOLATION_NETWORK_ONLY
    capabilities = REQUIRED_CAPABILITIES - {CAPABILITY_FILESYSTEM_ISOLATION,
                                            CAPABILITY_PID_ISOLATION}
    filesystem_isolated = False

    def __init__(self, unshare_path, python_path=None):
        self.unshare = unshare_path
        self.python = python_path or _sandbox_python()

    def _argv(self, limits):
        return [self.unshare, "-rn", "--", self.python, "-I", "-c", _NET_ONLY_INIT,
                str(limits.memory_mb), str(limits.cpu_seconds),
                str(limits.file_size_mb), str(limits.max_processes)]

    def run(self, request: RunRequest) -> RunOutcome:
        self._refuse_if_incomplete()
        self._refuse_if_unusable(request)
        started = time.time()
        workdir = tempfile.mkdtemp(prefix="waha-code-exec-")
        try:
            return self._spawn(request, workdir, started)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def _spawn(self, request, workdir, started):
        env = dict(CHILD_ENV)
        env["HOME"] = workdir
        env["TMPDIR"] = workdir
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            process = _spawn_process(self._argv(request.limits), request.code,
                                     workdir, env, out, err)
            timed_out = _wait(request.limits.timeout_seconds, process)
            return self._finish(request, process, out, err, started, timed_out)


class PrivateRootRunner(SandboxRunner):
    """The full boundary: mount + PID + network + user namespaces over a private root.

    `/` inside the sandbox is a tmpfs. The runtime is bound into it read-only;
    `/workspace` is the caller's directory (so files the program writes are the
    caller's to keep) and `/tmp` is scratch inside the root. Everything else on
    the host is not hidden by a rule -- it was never mounted, so it does not exist.
    """

    name = "private-root"
    isolation = ISOLATION_PRIVATE_ROOT
    capabilities = REQUIRED_CAPABILITIES
    filesystem_isolated = True

    def __init__(self, unshare_path, python_path=None, runtime_paths=()):
        self.unshare = unshare_path
        self.python = python_path or _sandbox_python()
        self.runtime_paths = tuple(runtime_paths)

    def _argv(self, limits, root, workspace):
        return [self.unshare, "-r", "-m", "-n", "-p", "-f", "--",
                self.python, "-I", "-c", INIT,
                root, workspace,
                str(limits.memory_mb), str(limits.cpu_seconds),
                str(limits.file_size_mb), str(limits.max_processes),
                *self.runtime_paths]

    def run(self, request: RunRequest) -> RunOutcome:
        self._refuse_if_incomplete()
        self._refuse_if_unusable(request)
        started = time.time()
        # One directory holds both halves -- the workspace the program writes into
        # and the root it is built from -- so a single removal reclaims everything
        # on every exit path, including the timeout path.
        base = tempfile.mkdtemp(prefix="waha-code-exec-")
        workspace = os.path.join(base, "workspace")
        root = os.path.join(base, "root")
        os.makedirs(workspace, exist_ok=True)
        try:
            return self._spawn(request, root, workspace, started)
        finally:
            shutil.rmtree(base, ignore_errors=True)

    def _spawn(self, request, root, workspace, started):
        env = dict(CHILD_ENV)
        env["HOME"] = "/workspace"
        env["TMPDIR"] = "/tmp"
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            process = _spawn_process(self._argv(request.limits, root, workspace),
                                     request.code, workspace, env, out, err)
            timed_out = _wait(request.limits.timeout_seconds, process)
            return self._finish(request, process, out, err, started, timed_out)


# The weaker bootstrap, kept beside the strong one so the difference between them
# is readable: this one applies limits and runs the program, and does nothing
# about the filesystem.
_NET_ONLY_INIT = (
    "import resource,sys\n"
    "mem,cpu,fsize,nproc=(int(a) for a in sys.argv[1:5])\n"
    "for r,v in [(resource.RLIMIT_AS,mem<<20),(resource.RLIMIT_CPU,cpu),\n"
    "            (resource.RLIMIT_FSIZE,fsize<<20),(resource.RLIMIT_NPROC,nproc),\n"
    "            (resource.RLIMIT_CORE,0)]:\n"
    "    resource.setrlimit(r,(v,v))\n"
    "src=sys.stdin.read()\n"
    "try:\n"
    "    exec(compile(src,'<code_exec>','exec'),{'__name__':'__main__'})\n"
    "except SystemExit:\n"
    "    raise\n"
    "except BaseException:\n"
    "    import traceback;traceback.print_exc();sys.exit(1)\n"
)


def _sandbox_python():
    """The interpreter the sandbox will be.

    The *base* interpreter, never a virtual environment: the sandbox offers the
    standard library, not the application's dependencies. A program that could
    import the app's packages could also read their configuration.
    """
    return os.path.realpath(getattr(sys, "_base_executable", None) or sys.executable)


def _runtime_paths():
    """Directories the runtime needs inside the root, read-only.

    `/usr` and friends cover a system interpreter; a toolchain interpreter
    (a CI runner, a pyenv, a hosted-toolcache build) lives elsewhere, so its base
    prefix and its own directory are added when they are outside the usual set.
    Everything named here is readable by the sandbox; nothing else is.
    """
    extra = []
    for candidate in (getattr(sys, "base_prefix", ""), os.path.dirname(_sandbox_python())):
        if candidate and candidate not in RUNTIME_PATHS and os.path.isdir(candidate):
            extra.append(candidate)
    return tuple(extra)


def _spawn_process(argv, code, workdir, env, out, err):
    # Output goes to unlinked temporary files rather than pipes: a pipe nobody
    # drains while the child is being awaited is how a parent deadlocks, or
    # buffers a runaway program's output in its own memory.
    process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=out, stderr=err,
                               cwd=workdir, env=env, start_new_session=True,
                               close_fds=True)
    try:
        process.stdin.write(code.encode("utf-8"))
        process.stdin.close()
    except (BrokenPipeError, OSError, ValueError):
        # The child died before reading its program; the exit status and stderr
        # below are the real answer, so this is not an error here.
        pass
    return process


def _wait(timeout_seconds, process):
    """Wait, then tear down the *whole* group. Returns whether it timed out."""
    try:
        process.wait(timeout=timeout_seconds)
        return False
    except subprocess.TimeoutExpired:
        _kill_group(process)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        return True


def _kill_group(process):
    """Kill the child *and everything it started*.

    `process.kill()` alone leaves grandchildren running: a program that spawned a
    helper would outlive its timeout. The child was started in its own session, so
    one signal to the group reaches all of them -- and inside a PID namespace the
    kernel reaps the rest when the namespace's init exits.
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


PROBE_PROGRAM = "print('waha-probe-ok')\n"


def _run_probe(runner, argv, workdir, env):
    """Run a probe with a trivial program; report whether the boundary held.

    `runner` has `subprocess.run` semantics, which is what makes the probes
    injectable: a test can hand in a callable that answers "refused" and prove the
    fallback chain without needing a kernel that actually refuses.
    """
    try:
        result = runner(argv, input=PROBE_PROGRAM.encode("utf-8"),
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        cwd=workdir, env=env, timeout=20, start_new_session=True)
    except subprocess.TimeoutExpired:
        return False, "the probe timed out"
    except OSError as error:
        return False, f"the probe could not run: {type(error).__name__}"
    stdout = (result.stdout or b"").decode("utf-8", "replace")
    if "waha-probe-ok" in stdout:
        return True, ""
    stderr = (result.stderr or b"").decode("utf-8", "replace")
    detail = (stderr.strip().splitlines() or [""])[-1][:200]
    return False, detail or f"exit {result.returncode}"


def probe_private_root(unshare_path, runner=subprocess.run, python=None):
    """Can this host build a private root? Asked by building one."""
    if not unshare_path:
        return None, "no `unshare` on this machine, so no namespaces at all"
    interpreter = python or _sandbox_python()
    boundary = PrivateRootRunner(unshare_path, interpreter, _runtime_paths())
    limits = Limits(timeout_seconds=10, memory_mb=256, cpu_seconds=5,
                    max_output_bytes=4000, max_input_bytes=4000)
    base = tempfile.mkdtemp(prefix="waha-probe-root-")
    workspace = os.path.join(base, "workspace")
    os.makedirs(workspace, exist_ok=True)
    env = dict(CHILD_ENV)
    env["HOME"] = "/workspace"
    env["TMPDIR"] = "/tmp"
    try:
        ok, detail = _run_probe(runner,
                                boundary._argv(limits, os.path.join(base, "root"), workspace),
                                base, env)
    finally:
        shutil.rmtree(base, ignore_errors=True)
    return (unshare_path, "") if ok else (None, f"a private root could not be built: {detail}")


def probe_network_namespace(unshare_path, runner=subprocess.run):
    """Does this host allow an unprivileged user + network namespace?

    Asked by *doing* it, because the answer depends on kernel policy (unprivileged
    user namespaces are restricted on some distributions) and not on whether the
    binary exists.
    """
    if not unshare_path:
        return None, "no `unshare` on this machine, so no network namespace"
    ok, detail = _run_probe(runner, [unshare_path, "-rn", "--", "true"],
                            tempfile.gettempdir(), dict(CHILD_ENV))
    if ok:
        return unshare_path, ""
    return None, "`unshare -rn` was refused by this kernel" + (f": {detail}" if detail else "")


_probe_cache = {}


def detect(which=shutil.which, runner=subprocess.run, use_cache=True):
    """The strongest runner this host can honestly offer.

    Private root first, then the network-only boundary, then a refusal -- and each
    step is probed empirically, because "the binary exists" and "the kernel allows
    it" are different questions. Cached because the probe forks and the answer
    cannot change while the service runs; tests pass `use_cache=False`.
    """
    if use_cache and "runner" in _probe_cache:
        return _probe_cache["runner"]
    unshare_path = which("unshare")
    if not unshare_path:
        decided = UnavailableRunner("no `unshare` on this machine, so no namespaces at all")
    else:
        path, reason = probe_private_root(unshare_path, runner=runner)
        if path:
            decided = PrivateRootRunner(path, _sandbox_python(), _runtime_paths())
        else:
            network_path, network_reason = probe_network_namespace(unshare_path, runner=runner)
            decided = (NamespaceRunner(network_path) if network_path
                       else UnavailableRunner("; ".join(filter(None, (reason, network_reason)))))
    if use_cache:
        _probe_cache["runner"] = decided
    return decided


def reset_cache():
    """Forget the probe. Used by tests; harmless in production."""
    _probe_cache.clear()
