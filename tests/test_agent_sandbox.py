"""R7 — the isolation boundary, tested by trying to break it.

The claim this file defends is narrow and checkable: *code execution in this
repository either runs inside a boundary that denies the network, scrubs the
environment and caps time, memory and output -- or it does not run at all.*

So these tests do not read the capability set and nod. They attempt the things the
boundary forbids: a real outbound connection, reading the parent's environment,
allocating past the ceiling, looping past the deadline, forking a helper that
would outlive the kill, printing past the cap. A boundary that stops them is
proven; a boundary that only says it would is caught here.

Two intentional asymmetries:

* The refusal path is tested unconditionally, because "no sandbox" must fail
  closed on every machine -- including the ones where namespaces are restricted.
* The enforcement tests skip with a printed reason when the machine cannot build
  the boundary. A skip is visible in the CI log rather than passing silently: a
  suite that quietly stops proving isolation is the failure mode this file exists
  to prevent, so `test_the_boundary_is_reported_not_assumed` always runs and says
  which of the two the run got.
"""
import json
import os
import subprocess
import sys
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


def limits(timeout=5, memory_mb=256, cpu=5, out=4000, inp=8000, processes=12):
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


class Enabled(AgentConfig):
    """The operator's opt-in, as a config the registry can read.

    Spelled out rather than assumed: the tool is off by default, so a test that
    reached it through the shipped `AgentConfig` would be testing the gate and
    not the boundary.
    """

    CODE_EXEC = True


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


class TheBoundaryIsReportedNotAssumed(unittest.TestCase):
    """Always runs. The two outcomes are 'enforced' and 'refused'; never silence."""

    def test_the_run_says_which_boundary_it_got(self):
        if BOUNDARY_AVAILABLE:
            self.assertTrue(RUNNER.supports())
            self.assertEqual(RUNNER.missing(), [])
            self.assertEqual(RUNNER.filesystem_isolated, False,
                             "the boundary must not claim filesystem isolation it lacks")
        else:
            # Not a failure: a machine that cannot build the boundary is a machine
            # where execution must refuse. The assertion is that the reason is
            # usable and the runner will not run anything.
            self.assertTrue(BOUNDARY_REASON)
            with self.assertRaises(sandbox.SandboxRefused) as caught:
                RUNNER.run(sandbox.RunRequest(code="print(1)", limits=limits()))
            self.assertEqual(caught.exception.code, "sandbox_unavailable")

    def test_a_refusal_names_what_is_missing_rather_than_failing_obscurely(self):
        class Partial(sandbox.NamespaceRunner):
            capabilities = sandbox.REQUIRED_CAPABILITIES - {sandbox.CAPABILITY_NO_NETWORK}

        runner = Partial("/usr/bin/unshare")
        self.assertEqual(runner.missing(), [sandbox.CAPABILITY_NO_NETWORK])
        self.assertFalse(runner.supports())
        with self.assertRaises(sandbox.SandboxRefused) as caught:
            runner.run(sandbox.RunRequest(code="print(1)", limits=limits()))
        self.assertEqual(caught.exception.code, "boundary_missing")
        self.assertIn("no_network", caught.exception.message)


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
        self.assertIn("refused", runner.reason)

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
        self.assertTrue(calls, "detect must *run* the probe, not trust `which`")
        self.assertLessEqual(calls[0][:2], ["/usr/bin/unshare", "-rn"])
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

    def test_an_oversized_program_is_refused_before_anything_runs(self):
        runner = sandbox.NamespaceRunner("/usr/bin/unshare")
        with self.assertRaises(sandbox.SandboxRefused) as caught:
            runner.run(sandbox.RunRequest(code="x = 1  # " + "y" * 5000,
                                          limits=limits(inp=100)))
        self.assertEqual(caught.exception.code, "input_too_large")

    def test_an_empty_program_is_refused_rather_than_run(self):
        runner = sandbox.NamespaceRunner("/usr/bin/unshare")
        with self.assertRaises(sandbox.SandboxRefused) as caught:
            runner.run(sandbox.RunRequest(code="   \n\t", limits=limits()))
        self.assertEqual(caught.exception.code, "empty_program")


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
                        "running code without an approval would bypass the one gate the loop has")
        self.assertFalse(tool.read_only)
        self.assertFalse(tool.network)

    def test_the_tool_refuses_with_a_reason_when_no_boundary_exists(self):
        runner = sandbox.UnavailableRunner("no `unshare` on this machine")
        with self.assertRaises(ToolError) as caught:
            call_code_exec({"code": "print(1)"}, runner=runner)
        self.assertIn("no `unshare` on this machine", str(caught.exception))

    def test_a_refused_call_is_an_error_the_loop_can_record(self):
        """`runtime._run_tool` catches ToolError and stores it as data; that contract
        is what keeps a refused execution from killing the task."""
        runner = sandbox.UnavailableRunner("no boundary")
        with self.assertRaises(ToolError):
            call_code_exec({"code": "print(1)"}, runner=runner)

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
                "print(len(names), names)\n",
                timeout=10)
        finally:
            os.environ.pop(marker, None)
        self.assertIn("CLEAN", outcome.stdout)
        self.assertNotIn(marker, outcome.stdout)
        self.assertNotIn("leaked-if-visible", outcome.stdout)
        # The allowlist is an allowlist: a handful of literals, not a filtered copy.
        self.assertLess(int(outcome.stdout.splitlines()[1].split()[0]), 10)

    def test_a_program_that_never_ends_is_killed_at_the_deadline(self):
        started = time.time()
        outcome = run_code("while True:\n    pass\n", timeout=2, cpu=30)
        elapsed = time.time() - started
        self.assertTrue(outcome.timed_out)
        self.assertLess(elapsed, 15, "the deadline must bound the wall clock too")
        self.assertIn("timed out", outcome.error)
        self.assertNotEqual(outcome.exit_status, 0)

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

    def test_output_is_truncated_at_the_cap_and_says_so(self):
        outcome = run_code("print('x' * 200000)", timeout=10, out=2000)
        self.assertTrue(outcome.truncated)
        self.assertLessEqual(len(outcome.stdout.encode("utf-8")), 2000)

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

    def test_each_run_gets_its_own_directory_and_it_is_removed_afterwards(self):
        before = _exec_dirs()
        outcome = run_code("import os\nprint(os.getcwd())\n", timeout=10)
        self.assertTrue(outcome.ok)
        used = outcome.stdout.strip()
        self.assertNotIn(str(ROOT), used, "the run should not execute inside the project")
        self.assertEqual(_exec_dirs() - before, set(),
                         "the run directory outlived the run")

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
        self.assertFalse(outcome["filesystem_isolated"],
                         "the boundary must not claim a filesystem guarantee it lacks")
        self.assertEqual(outcome["isolation"], sandbox.ISOLATION_NAMESPACE)
        self.assertIn("note", outcome)
        self.assertIsInstance(json.loads(json.dumps(outcome)), dict)

    def test_the_tool_uses_the_configured_limits(self):
        """The knobs are the ceilings the run actually receives."""

        class Tight(Enabled):
            CODE_EXEC_TIMEOUT_SECONDS = 2
            CODE_EXEC_MEMORY_MB = 128
            CODE_EXEC_CPU_SECONDS = 5
            CODE_EXEC_MAX_OUTPUT_BYTES = 500

        outcome = call_code_exec({"code": "print('y' * 50000)"}, config=Tight)
        self.assertTrue(outcome["truncated"])
        self.assertLessEqual(len(outcome["stdout"].encode("utf-8")), 500)

    def test_work_is_bounded_by_wall_clock_not_just_by_cpu(self):
        """A program that sleeps forever burns no CPU but must still be stopped."""
        started = time.time()
        outcome = run_code("import time\ntime.sleep(600)\n", timeout=2, cpu=30)
        self.assertTrue(outcome.timed_out)
        self.assertLess(time.time() - started, 15)


def _exec_dirs():
    import glob
    import tempfile
    return set(glob.glob(os.path.join(tempfile.gettempdir(), "waha-code-exec-*")))


if __name__ == "__main__":
    unittest.main()
