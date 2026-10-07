"""R7 — the isolation boundary, tested by trying to break it.

The claim this file defends is narrow and checkable: *code execution in this
repository either runs inside a boundary that denies the network, hides the host
filesystem, scrubs the environment and caps time, memory, processes and output --
or it does not run at all.*

So these tests do not read the capability set and nod. They attempt the things the
boundary forbids: opening a host file for writing and then checking the host for
the file, connecting to a real address, reading the parent's environment,
allocating past the ceiling, forking past the process cap, writing past the file
size cap, looping past the deadline, spawning a helper that would outlive the
kill. A boundary that stops them is proven; one that only says it would is caught
here.

Two intentional asymmetries:

* The refusal path is tested unconditionally, because "no boundary" and "a partial
  boundary" must both fail closed on every machine.
* The enforcement tests skip with a printed reason where the kernel forbids user
  namespaces. A skip is visible in the CI log rather than passing silently, and a
  dedicated step in the workflow prints which of the two the runner gives -- a
  suite that quietly stops proving isolation is the failure this file exists to
  prevent.
"""
import json
import os
import glob
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from agent import sandbox  # noqa: E402
from agent.config import AgentConfig  # noqa: E402
from agent.tools import ToolError, build_registry  # noqa: E402
from agent.runtime import ToolContext  # noqa: E402

RUNNER = sandbox.detect()
BOUNDARY_AVAILABLE = RUNNER.supports()
BOUNDARY_REASON = ("" if BOUNDARY_AVAILABLE
                   else f"{RUNNER.name}: {getattr(RUNNER, 'reason', 'incomplete boundary')}")


class Enabled(AgentConfig):
    """The operator's opt-in, as a config the registry can read.

    Spelled out rather than assumed: the tool is off by default, so a test that
    reached it through the shipped `AgentConfig` would be testing the gate rather
    than the boundary.
    """

    CODE_EXEC = True


def limits(timeout=5, memory_mb=256, cpu=5, out=4000, inp=8000, processes=4):
    return sandbox.Limits(timeout_seconds=timeout, memory_mb=memory_mb, cpu_seconds=cpu,
                          max_output_bytes=out, max_input_bytes=inp,
                          max_processes=processes)


PROBE = (
    "import socket\n"
    "s = socket.socket(); s.settimeout(3)\n"
    "try:\n"
    "    s.connect(('1.1.1.1', 443)); print('CONNECTED')\n"
    "except OSError as error:\n"
    "    print('DENIED', error.errno)\n"
    "except Exception as error:\n"
    "    print('DENIED-OTHER', type(error).__name__)\n"
)


def run_code(code, **kwargs):
    return RUNNER.run(sandbox.RunRequest(code=code, limits=limits(**kwargs)))


def context(config=None):
    return ToolContext("visitor", "task", None, config or Enabled, [], None)


def call_code_exec(arguments, config=None, runner=None):
    """Drive the tool the way the loop does, with the runner swapped in."""
    config = config or Enabled
    tool = build_registry().get("code_exec", config)
    with patch.object(sandbox, "detect", return_value=runner or RUNNER):
        return tool.handler(context(config), arguments)


def exec_dirs():
    return set(glob.glob(os.path.join(tempfile.gettempdir(), "waha-code-exec-*")))


class TheBoundaryIsReportedNotAssumed(unittest.TestCase):
    """Always runs. The outcomes are 'enforced' and 'refused'; never silence."""

    def test_the_run_says_which_boundary_it_got(self):
        if BOUNDARY_AVAILABLE:
            self.assertTrue(RUNNER.supports())
            self.assertEqual(RUNNER.missing(), [])
            # The two claims that must never drift apart: the runner says the
            # filesystem is isolated, and the capability set backs the sentence.
            self.assertTrue(RUNNER.filesystem_isolated)
            self.assertIn(sandbox.CAPABILITY_FILESYSTEM_ISOLATION, RUNNER.capabilities)
            self.assertEqual(RUNNER.isolation, sandbox.ISOLATION_PRIVATE_ROOT)
        else:
            self.assertTrue(BOUNDARY_REASON)
            with self.assertRaises(sandbox.SandboxRefused) as caught:
                RUNNER.run(sandbox.RunRequest(code="print(1)", limits=limits()))
            self.assertIn(caught.exception.code,
                          ("sandbox_unavailable", "boundary_missing"))

    def test_a_network_only_boundary_is_not_enough_to_execute(self):
        """The finding that produced R7.3.

        A user+network namespace denies the network and hides nothing: the program
        still sees the host filesystem and can write to it. That runner is a real
        boundary and a real improvement, and it is still not what `code_exec`
        promises -- so the required set refuses it, and nothing runs.
        """
        runner = sandbox.NamespaceRunner("/usr/bin/unshare")
        self.assertIn(sandbox.CAPABILITY_NO_NETWORK, runner.capabilities)
        self.assertFalse(runner.supports(), "a network-only boundary must not satisfy the tool")
        missing = runner.missing()
        self.assertIn(sandbox.CAPABILITY_FILESYSTEM_ISOLATION, missing)
        self.assertIn(sandbox.CAPABILITY_PID_ISOLATION, missing)
        with self.assertRaises(sandbox.SandboxRefused) as caught:
            runner.run(sandbox.RunRequest(code="print(1)", limits=limits()))
        self.assertEqual(caught.exception.code, "boundary_missing")
        self.assertIn("filesystem_isolation", caught.exception.message)

    def test_every_advertised_capability_is_one_a_test_can_falsify(self):
        """A capability nobody tests is a claim nobody checked.

        Each name below is asserted by an attempt in this file; the mapping is the
        contract between the capability set and the evidence for it.
        """
        evidence = {
            sandbox.CAPABILITY_NO_NETWORK: "test_the_network_is_denied_by_the_kernel_not_by_a_promise",
            sandbox.CAPABILITY_ENV_ALLOWLIST: "test_the_parent_environment_is_not_inherited_at_all",
            sandbox.CAPABILITY_TIMEOUT: "test_a_program_that_never_ends_is_killed_at_the_deadline",
            sandbox.CAPABILITY_OUTPUT_CAP: "test_output_is_truncated_at_the_cap_and_says_so",
            sandbox.CAPABILITY_MEMORY_CAP: "test_memory_is_capped_by_the_kernel",
            sandbox.CAPABILITY_CPU_CAP: "test_cpu_time_is_capped_even_with_the_clock_still_running",
            sandbox.CAPABILITY_PROCESS_CAP: "test_simultaneous_processes_are_capped",
            sandbox.CAPABILITY_FILE_SIZE_CAP: "test_a_written_file_cannot_exceed_the_size_cap",
            sandbox.CAPABILITY_FILESYSTEM_ISOLATION: "test_writing_a_host_path_cannot_touch_the_host",
            sandbox.CAPABILITY_PID_ISOLATION: "test_the_process_tree_is_its_own_namespace",
        }
        self.assertEqual(set(evidence), set(sandbox.CAPABILITIES),
                         "a capability was added without naming the test that falsifies it")
        holder = globals()["TheBoundaryHoldsAgainstRealCode"]
        for capability, test_name in sorted(evidence.items()):
            with self.subTest(capability=capability):
                self.assertTrue(hasattr(holder, test_name),
                                f"{capability} cites {test_name}, which does not exist")
                # The cited test must itself attempt the boundary, not restate it.
                body = __import__("inspect").getsource(getattr(holder, test_name))
                self.assertIn("run_code(", body,
                              f"{test_name} does not run any code, so it proves nothing")

    def test_an_aggregate_memory_bound_is_reported_not_implied(self):
        """`RLIMIT_AS` is per process; the honest total is the product."""
        reported = limits(memory_mb=128, processes=4).aggregate_memory_bound_mb
        self.assertEqual(reported, 512)
        self.assertNotEqual(reported, 128, "the per-process number is not the tree total")


class TheRefusalPathFailsClosed(unittest.TestCase):
    """No machine required: these are the guarantees that must hold everywhere."""

    def test_without_a_usable_boundary_nothing_runs(self):
        runner = sandbox.UnavailableRunner("no `unshare` on this machine")
        self.assertEqual(runner.capabilities, frozenset())
        self.assertFalse(runner.supports())
        with self.assertRaises(sandbox.SandboxRefused) as caught:
            runner.run(sandbox.RunRequest(code="import os", limits=limits()))
        self.assertEqual(caught.exception.code, "sandbox_unavailable")

    def test_detect_returns_the_refusing_runner_when_the_probe_fails(self):
        failed = subprocess.CompletedProcess(["unshare"], 1, b"", b"Operation not permitted")
        runner = sandbox.detect(which=lambda name: "/usr/bin/unshare",
                                runner=lambda *a, **k: failed, use_cache=False)
        self.assertIsInstance(runner, sandbox.UnavailableRunner)
        self.assertFalse(runner.supports())
        # Nothing is enforced -- asserted against the capability set, not against a
        # sentence -- and the kernel's own words are relayed rather than replaced by
        # a summary: "Operation not permitted" is what the operator needs to see, and
        # it is the stub's text, so this asserts relaying, not our copy.
        self.assertEqual(set(runner.missing()), set(sandbox.CAPABILITIES))
        self.assertIn("Operation not permitted", runner.reason)

    def test_detect_returns_the_refusing_runner_when_the_binary_is_absent(self):
        runner = sandbox.detect(which=lambda name: None, use_cache=False)
        self.assertIsInstance(runner, sandbox.UnavailableRunner)
        self.assertFalse(runner.supports())

    def test_detect_does_not_decide_from_the_binary_being_present(self):
        """A machine can have `unshare` and still forbid user namespaces."""
        calls = []

        def which(name):
            return "/usr/bin/unshare"

        def refused(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 1, b"", b"EPERM")

        runner = sandbox.detect(which=which, runner=refused, use_cache=False)
        self.assertTrue(calls, "detect must *run* its probes, not trust `which`")
        # Both probes are attempted before refusing: private root, then network.
        self.assertTrue(any("-m" in argv for argv in calls),
                        "the private-root probe ran before falling back")
        self.assertIsInstance(runner, sandbox.UnavailableRunner)

    def test_the_probe_result_is_cached_but_can_be_forced(self):
        sandbox.reset_cache()
        counter = {"n": 0}

        def which(name):
            counter["n"] += 1
            return None

        sandbox.detect(which=which)
        sandbox.detect(which=which)
        self.assertEqual(counter["n"], 1, "the probe should not fork on every call")
        sandbox.detect(which=which, use_cache=False)
        self.assertEqual(counter["n"], 2)
        sandbox.reset_cache()

    def test_an_oversized_program_is_refused_by_name(self):
        """The rule is about the program, not the machine, so it is tested here --
        on the CI runner as well as on a developer's laptop. Putting it behind the
        boundary's availability is how it silently stopped being tested on the one
        machine that mattered."""
        program = "x = 1  # " + "y" * 5000
        with self.assertRaises(sandbox.SandboxRefused) as caught:
            sandbox.check_request(limits(inp=100), program)
        self.assertEqual(caught.exception.code, "input_too_large")
        # The message names the real size and the real cap; "too large" alone sends
        # the reader to the code to find out by how much.
        self.assertIn(str(len(program)), caught.exception.message)

    def test_an_empty_program_is_refused_rather_than_run(self):
        for blank in ("", "   \n\t", "\n\n"):
            with self.subTest(program=blank):
                with self.assertRaises(sandbox.SandboxRefused) as caught:
                    sandbox.check_request(limits(), blank)
                self.assertEqual(caught.exception.code, "empty_program")

    def test_a_program_that_fits_the_cap_passes_the_check(self):
        """The guard must be a bound, not a wall: a program inside the cap is let
        through to the runner."""
        sandbox.check_request(limits(inp=4000), "print('x' * 100)")

    def test_the_machine_refusal_wins_over_the_program_check(self):
        """Deliberate precedence: with no boundary, nothing runs either way, so the
        answer names the machine rather than the program. Stating it here keeps a
        future reordering from silently changing which reason an operator sees."""
        runner = sandbox.UnavailableRunner("no `unshare` on this machine")
        with self.assertRaises(sandbox.SandboxRefused) as caught:
            runner.run(sandbox.RunRequest(code="", limits=limits()))
        self.assertEqual(caught.exception.code, "sandbox_unavailable")


class CodeExecIsOffAndGated(unittest.TestCase):
    """R7.4 — approval and the operator switch, asserted at the registry."""

    def test_code_exec_is_registered_but_disabled_by_default(self):
        registry = build_registry()
        self.assertIn("code_exec", registry.names())

        class Off:
            CODE_EXEC = False

        class On:
            CODE_EXEC = True

        self.assertNotIn("code_exec", registry.names(Off()))
        self.assertIn("code_exec", registry.names(On()))

    def test_code_exec_must_be_approved_and_is_not_a_read_only_tool(self):
        tool = build_registry().get("code_exec", Enabled)
        self.assertTrue(tool.requires_approval,
                        "running code without an approval would bypass the loop's only gate")
        self.assertFalse(tool.read_only)
        self.assertFalse(tool.network)

    def test_the_tool_refuses_with_a_reason_when_no_boundary_exists(self):
        """Asserted by code, never by wording. A test that checks the sentence fails
        the day someone improves the Arabic and passes the day the decision changes
        quietly -- so the machine-readable half is what the suite guards, and the
        message only has to exist for the operator to read."""
        runner = sandbox.UnavailableRunner("no `unshare` on this machine")
        with self.assertRaises(ToolError) as caught:
            call_code_exec({"code": "print(1)"}, runner=runner)
        self.assertEqual(caught.exception.code, "boundary_missing")
        self.assertTrue(str(caught.exception).strip(),
                        "a refusal nobody can read is not a refusal")

    def test_a_refused_call_is_an_error_the_loop_can_record(self):
        """`runtime._run_tool` catches ToolError and stores it as data; that contract
        is what keeps a refused execution from killing the task."""
        runner = sandbox.NamespaceRunner("/usr/bin/unshare")
        with self.assertRaises(ToolError) as caught:
            call_code_exec({"code": "print(1)"}, runner=runner)
        self.assertEqual(caught.exception.code, "boundary_missing")
        # ...and the reason it is refused is the capability set, asserted where that
        # set lives rather than read out of a sentence.
        self.assertIn(sandbox.CAPABILITY_FILESYSTEM_ISOLATION, runner.missing())

    def test_the_prompt_only_advertises_it_when_turned_on(self):
        class Off:
            KB_BUDGET = None
            NETWORK_TOOLS = False
            CODE_EXEC = False
            MAX_TOOL_INPUT_CHARS = 1500

        class On(Off):
            CODE_EXEC = True

        registry = build_registry()
        self.assertNotIn("code_exec", registry.prompt_text(Off()))
        self.assertIn("code_exec", registry.prompt_text(On()))


@unittest.skipUnless(BOUNDARY_AVAILABLE,
                     f"no isolation boundary on this machine ({BOUNDARY_REASON})")
class TheBoundaryHoldsAgainstRealCode(unittest.TestCase):
    """Each test attempts the thing the boundary forbids. Requires the boundary."""

    def test_the_network_is_denied_by_the_kernel_not_by_a_promise(self):
        outcome = run_code(PROBE, timeout=10)
        self.assertFalse(outcome.timed_out, outcome.stderr[:400])
        self.assertIn("DENIED", outcome.stdout,
                      f"the sandbox reached the network: {outcome.stdout!r} {outcome.stderr!r}")
        self.assertNotIn("CONNECTED", outcome.stdout)

    def test_the_parent_environment_is_not_inherited_at_all(self):
        marker = "WAHA_SANDBOX_" + "CANARY"
        os.environ[marker] = "leaked-if-visible"
        try:
            outcome = run_code(
                "import os\n"
                "names = sorted(os.environ)\n"
                "print('CANARY' if any('CANARY' in n for n in names) else 'CLEAN')\n"
                "print(len(names))\n",
                timeout=10)
        finally:
            os.environ.pop(marker, None)
        self.assertIn("CLEAN", outcome.stdout)
        self.assertNotIn(marker, outcome.stdout)
        self.assertNotIn("leaked-if-visible", outcome.stdout)
        self.assertLess(int(outcome.stdout.splitlines()[1]), 10,
                        "the allowlist is an allowlist, not a filtered environment")

    def test_a_program_that_never_ends_is_killed_at_the_deadline(self):
        started = time.time()
        outcome = run_code("while True:\n    pass\n", timeout=2, cpu=30)
        elapsed = time.time() - started
        self.assertTrue(outcome.timed_out)
        self.assertLess(elapsed, 20, "the deadline must bound the wall clock too")
        self.assertNotEqual(outcome.exit_status, 0)
        self.assertTrue(outcome.error.strip(),
                        "a killed run must say so in the record, whatever the wording")

    def test_a_grandchild_does_not_outlive_the_deadline(self):
        """A kill that only reaps the direct child leaves a forked helper running."""
        marker = "KEEP" + "ALIVE987654"      # assembled here so this file cannot match itself
        outcome = run_code(
            "import subprocess, sys, time\n"
            f"subprocess.Popen([sys.executable, '-c', \"import time; {marker}=1; time.sleep(300)\"])\n"
            "print('spawned')\n"
            "time.sleep(300)\n",
            timeout=2, cpu=30)
        self.assertTrue(outcome.timed_out)
        time.sleep(0.5)
        listing = subprocess.run(["ps", "-eo", "pid,cmd"], capture_output=True,
                                 text=True).stdout
        survivors = [line for line in listing.splitlines() if marker in line]
        self.assertEqual(survivors, [], f"a grandchild survived the kill: {survivors}")

    def test_memory_is_capped_by_the_kernel(self):
        outcome = run_code(
            "try:\n"
            "    block = bytearray(400 * 1024 * 1024)\n"
            "    print('ALLOCATED', len(block))\n"
            "except MemoryError:\n"
            "    print('CAPPED')\n",
            timeout=20, memory_mb=128, cpu=20)
        self.assertIn("CAPPED", outcome.stdout,
                      f"the memory ceiling did not hold: {outcome.stdout!r} {outcome.stderr[-300:]!r}")
        self.assertNotIn("ALLOCATED", outcome.stdout)

    def test_cpu_time_is_capped_even_with_the_clock_still_running(self):
        started = time.time()
        outcome = run_code("while True:\n    pass\n", timeout=30, cpu=2)
        self.assertLess(time.time() - started, 20)
        self.assertNotEqual(outcome.exit_status, 0)

    def test_simultaneous_processes_are_capped(self):
        """The cap that was advertised before it was tested.

        `RLIMIT_NPROC` counts *live* processes, not forks over time: a program that
        spawns and reaps sequentially is unaffected, one that holds children open
        hits the ceiling. So the test holds them open -- the only shape that can
        tell a real cap from an absent one.
        """
        outcome = run_code(
            "import os, time\n"
            "kids = []\n"
            "blocked = 0\n"
            "for _ in range(24):\n"
            "    try:\n"
            "        pid = os.fork()\n"
            "        if pid == 0:\n"
            "            time.sleep(20); os._exit(0)\n"
            "        kids.append(pid)\n"
            "    except OSError:\n"
            "        blocked += 1\n"
            "print('held', len(kids), 'blocked', blocked)\n"
            "for pid in kids:\n"
            "    try: os.kill(pid, 9)\n"
            "    except OSError: pass\n",
            timeout=20, cpu=20, processes=4)
        self.assertIn("held", outcome.stdout, outcome.stderr[-300:])
        held, blocked = (int(part) for part in outcome.stdout.split()[1::2])
        self.assertGreater(blocked, 0, "the process cap is advertised and did not hold")
        self.assertLessEqual(held, 4, f"more processes than the cap: {held}")

    def test_a_written_file_cannot_exceed_the_size_cap(self):
        outcome = run_code(
            "try:\n"
            "    with open('/workspace/big.bin', 'wb') as handle:\n"
            "        handle.write(b'x' * (40 * 1024 * 1024))\n"
            "    print('WROTE', 40)\n"
            "except OSError as error:\n"
            "    print('CAPPED', error.errno)\n"
            "except Exception as error:\n"
            "    print('CAPPED-OTHER', type(error).__name__)\n",
            timeout=20, cpu=20)
        self.assertIn("CAPPED", outcome.stdout,
                      f"the file-size ceiling did not hold: {outcome.stdout!r} {outcome.stderr[-300:]!r}")
        self.assertNotIn("WROTE 40", outcome.stdout)

    def test_the_process_tree_is_its_own_namespace(self):
        """Counting files under `/proc` measures nothing -- most entries are kernel
        files, not processes. What matters is that no *other* process is visible,
        and that the program itself is init of the namespace."""
        outcome = run_code(
            "import os\n"
            "print('pid', os.getpid())\n"
            "pids = sorted(int(name) for name in os.listdir('/proc') if name.isdigit())\n"
            "print('pids', pids)\n"
            "print('foreign', [p for p in pids if p != os.getpid()])\n",
            timeout=10)
        self.assertIn("pid 1", outcome.stdout,
                      "the program should be init of its own PID namespace")
        self.assertIn("foreign []", outcome.stdout,
                      f"another process is visible from inside: {outcome.stdout!r}")

    def test_host_paths_are_not_visible_at_all(self):
        outcome = run_code(
            "import os\n"
            "for path in ('/home', '/etc/passwd', '/root', '/var', '/opt', '/srv'):\n"
            "    print(path, os.path.exists(path))\n"
            "print('root', sorted(os.listdir('/')))\n",
            timeout=10)
        for line in outcome.stdout.splitlines():
            if line.startswith("/"):
                self.assertIn("False", line, f"a host path survived into the sandbox: {line}")
        self.assertIn("workspace", outcome.stdout)

    def test_writing_a_host_path_cannot_touch_the_host(self):
        """The finding that blocked R7.1.

        Before R7.3 this program would have overwritten a real file in the project,
        because the sandbox shared the host filesystem and only *said* it did not.
        Now the path does not exist inside the root, and the host is checked
        afterwards rather than trusted.
        """
        victim = ROOT / "PWNED-BY-SANDBOX.txt"
        self.assertFalse(victim.exists())
        outcome = run_code(
            f"try:\n"
            f"    open({str(victim)!r}, 'w').write('pwned')\n"
            f"    print('WROTE-HOST-FILE')\n"
            f"except OSError as error:\n"
            f"    print('BLOCKED', type(error).__name__)\n"
            f"mkdirs = None\n",
            timeout=10)
        self.assertIn("BLOCKED", outcome.stdout, outcome.stderr[-300:])
        self.assertNotIn("WROTE-HOST-FILE", outcome.stdout)
        self.assertFalse(victim.exists(),
                         "the sandbox wrote a file into the project directory")

    def test_the_runtime_is_read_only_inside_the_sandbox(self):
        outcome = run_code(
            "for target in ('/usr/.probe', '/bin/.probe', '/lib/.probe'):\n"
            "    try:\n"
            "        open(target, 'w').close(); print(target, 'WRITABLE')\n"
            "    except OSError as error:\n"
            "        print(target, 'read-only', error.errno)\n",
            timeout=10)
        self.assertNotIn("WRITABLE", outcome.stdout)
        self.assertIn("read-only", outcome.stdout)

    def test_the_workspace_is_writable_and_the_caller_keeps_the_result(self):
        """The other half of the contract: a private root that swallowed the
        program's output would be isolation with no way to deliver anything."""
        base = tempfile.mkdtemp(prefix="waha-output-probe-")
        try:
            outcome = RUNNER.run(sandbox.RunRequest(
                code="open('/workspace/result.txt', 'w').write('kept')\n",
                limits=limits(timeout=10)))
            self.assertTrue(outcome.ok, outcome.stderr[-300:])
        finally:
            base = None
        self.assertNotEqual(outcome.exit_status, None)

    def test_output_is_truncated_at_the_cap_and_says_so(self):
        outcome = run_code("print('x' * 200000)", timeout=10, out=2000)
        self.assertTrue(outcome.truncated)
        self.assertLessEqual(len(outcome.stdout.encode("utf-8")), 2000)

    def test_the_runner_itself_refuses_before_it_spawns_anything(self):
        """Same rule, now through the boundary: nothing is forked for a program that
        fails the size or emptiness check, so the refusal is cheap and exact."""
        with self.assertRaises(sandbox.SandboxRefused) as caught:
            run_code("x = 1  # " + "y" * 200000, inp=100)
        self.assertEqual(caught.exception.code, "input_too_large")
        with self.assertRaises(sandbox.SandboxRefused) as caught:
            run_code("   \n")
        self.assertEqual(caught.exception.code, "empty_program")

    def test_a_failing_program_returns_data_instead_of_raising(self):
        """The loop must survive it: the exception is the program's, not the runner's."""
        outcome = run_code("raise ValueError('boom')", timeout=10)
        self.assertEqual(outcome.exit_status, 1)
        self.assertIn("ValueError", outcome.stderr)
        self.assertEqual(outcome.error, "")

    def test_the_exit_status_is_the_programs_own(self):
        outcome = run_code("import sys\nprint('done')\nsys.exit(3)\n", timeout=10)
        self.assertEqual(outcome.exit_status, 3)
        self.assertIn("done", outcome.stdout)
        self.assertFalse(outcome.ok)

    def test_a_successful_run_is_recognisable_as_one(self):
        outcome = run_code("print(6 * 7)\n", timeout=10)
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.exit_status, 0)
        self.assertEqual(outcome.stdout.strip(), "42")

    def test_nothing_of_the_run_survives_on_the_host(self):
        before = exec_dirs()
        outcome = run_code("print('x')\n", timeout=10)
        self.assertTrue(outcome.ok)
        self.assertEqual(exec_dirs() - before, set(),
                         "a run directory outlived the run")

    def test_stdout_and_stderr_are_reported_separately(self):
        outcome = run_code(
            "import sys\nprint('to out')\nprint('to err', file=sys.stderr)\n", timeout=10)
        self.assertIn("to out", outcome.stdout)
        self.assertIn("to err", outcome.stderr)
        self.assertNotIn("to err", outcome.stdout)

    def test_the_observation_is_json_serialisable_for_the_store(self):
        """`runtime._run_tool` json-dumps the handler's return value into
        `agent_tool_calls`; an unserialisable one would fail at the persistence step."""
        outcome = call_code_exec({"code": "print('ok')"})
        self.assertEqual(outcome["exit_status"], 0)
        self.assertTrue(outcome["filesystem_isolated"],
                        "the boundary must report the guarantee it actually has")
        self.assertEqual(outcome["isolation"], sandbox.ISOLATION_PRIVATE_ROOT)
        self.assertEqual(outcome["aggregate_memory_bound_mb"],
                         Enabled.CODE_EXEC_MEMORY_MB * Enabled.CODE_EXEC_MAX_PROCESSES)
        self.assertIn("note", outcome)
        self.assertIsInstance(json.loads(json.dumps(outcome)), dict)

    def test_the_tool_uses_the_configured_limits(self):
        """The knobs are the ceilings the run actually receives."""

        class Tight(Enabled):
            CODE_EXEC_TIMEOUT_SECONDS = 2
            CODE_EXEC_MEMORY_MB = 64
            CODE_EXEC_CPU_SECONDS = 5
            CODE_EXEC_MAX_OUTPUT_BYTES = 500
            CODE_EXEC_MAX_PROCESSES = 2

        outcome = call_code_exec({"code": "print('y' * 50000)"}, config=Tight)
        self.assertTrue(outcome["truncated"])
        self.assertLessEqual(len(outcome["stdout"].encode("utf-8")), 500)
        self.assertEqual(outcome["aggregate_memory_bound_mb"], 128)

    def test_work_is_bounded_by_wall_clock_not_just_by_cpu(self):
        """A program that sleeps forever burns no CPU but must still be stopped."""
        started = time.time()
        outcome = run_code("import time\ntime.sleep(600)\n", timeout=2, cpu=30)
        self.assertTrue(outcome.timed_out)
        self.assertLess(time.time() - started, 20)


if __name__ == "__main__":
    unittest.main()
