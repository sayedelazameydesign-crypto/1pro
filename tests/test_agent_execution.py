"""R6: one agent, two execution modes, one contract.

The claim is deliberately narrow: moving between Render, Vercel and the PromptQL gateway
may change *where* the loop runs and *how long* it may run, and nothing else. So the
central test here runs one scripted task through the queued path and the inline path and
compares semantics field by field -- plan, steps, tool calls, status, event names, report
-- while wall-clock and row ids are allowed to differ.

All of it is offline: a real `Store` on a temp SQLite file, the real tool registry,
`FakeProvider` as the model. Flask is not imported, because a mode is not a web concern:
`app.py` only translates deployment facts into the policy these tests hand in.
"""
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from agent import execution  # noqa: E402
from agent.config import AgentConfig  # noqa: E402
from agent.providers import FakeProvider, ProviderError  # noqa: E402
from agent.runtime import Deps as AgentDeps  # noqa: E402
from agent.service import TERMINAL, Service  # noqa: E402
from agent.store import Store  # noqa: E402
from agent.tools import Registry, Tool, build_registry  # noqa: E402


class Conf(AgentConfig):
    """The real config with test-stable numbers.

    Subclassing instead of inventing a fake means every knob the runtime reads exists
    with a production default, so the only thing under test is the cap itself -- a
    hand-written config would also be testing my memory of the defaults.
    """
    MAX_STEPS = 4
    MAX_AI_CALLS = 8
    MAX_TOOL_CALLS = 6
    DEADLINE_SECONDS = 180
    PROVIDER_TIMEOUT_SECONDS = 75
    WORKERS = 1
    APPROVAL_TIMEOUT_SECONDS = 60
    NETWORK_TOOLS = False


class Tight(Conf):
    """Already below every platform cap, to prove a cap may only tighten."""
    MAX_AI_CALLS = 1


def sqlite_store(path):
    class DB:
        postgres = False

        @staticmethod
        def connect():
            conn = sqlite3.connect(path, timeout=20)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            return conn

        @staticmethod
        def run(db, sql, params=()):
            return db.execute(sql, params)

        @staticmethod
        def insert_returning_id(db, sql, params=()):
            return db.execute(sql, params).lastrowid

    store = Store(DB())
    store.apply_schema()
    # `attempts` belongs to the app schema, not the agent schema: the loop books every
    # provider call into the same table the chat path uses, which is how one visitor gets
    # one hourly budget. An isolated harness has to provide it, or it tests a runtime with
    # no rate limit -- a different product.
    with store.db.connect() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS attempts("
                     "user_id TEXT NOT NULL, created_at REAL NOT NULL)")
    return store


def plan(titles):
    return {"steps": [{"title": title, "goal": title + " بالتفصيل"} for title in titles]}


def action(tool, **args):
    return {"thought": "أحتاج الأداة", "action": {"tool": tool, "args": args}, "final": None}


def final(text):
    return {"thought": "كفى", "action": None, "final": text}


# Six provider turns: plan, (act, answer) per step, report. `clock` and `calculator` are
# read-only, ungated, network-free tools -- exactly the ones that must behave the same in
# a request and in a worker.
SCRIPT = [plan(["احسب المتوسط", "اكتب الخلاصة"]),
          action("calculator", expression="(12+18+24)/3"),
          final("المتوسط 18"),
          action("clock", offset_days=0),
          final("أُرفق التاريخ"),
          "انتهت المهمة: المتوسط 18 مع التاريخ."]

VOLATILE_EVENT_KEYS = ("step_id", "call_id", "task_id", "elapsed_ms")
VOLATILE_ARTIFACT_KEYS = ("id", "updated_at", "bytes")


class Case(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = sqlite_store(str(Path(temp.name) / "exec.db"))
        self.tools = build_registry()
        self.ran = []

    # -- harness --------------------------------------------------------------
    def policy(self, *, serverless=False, promptql=False, config=Conf):
        return execution.resolve(ai_mode="promptql" if promptql else "gemini",
                                 serverless=serverless, config=config)

    def deps(self, policy, script=None, hooks=None):
        calls = {"n": 0}
        self.provider_calls = calls

        def provider_factory(task=None):
            calls["n"] += 1
            return FakeProvider(model="fake", script=list(SCRIPT if script is None else script))

        return AgentDeps(store=self.store, config=policy.config or Conf, tools=self.tools,
                         provider_factory=provider_factory, skills=[],
                         limits={"user_ai_per_hour": 30, "ip_ai_per_hour": 120},
                         hooks=dict(hooks or {}), inline=policy.is_inline,
                         rag_index_dir=ROOT / "data" / "rag")

    def start_task(self, goal="احسب متوسط الأرقام ثم اكتب الخلاصة"):
        return self.store.create_task("u_exec", goal, "gemini", "fake",
                                      time.time() + Conf.DEADLINE_SECONDS)

    def run_task(self, policy, script=None, hooks=None):
        task_id = self.start_task()
        service = Service(self.deps(policy, script, hooks), mode=policy.mode)
        if policy.mode == execution.QUEUED:
            service.submit(task_id)
            deadline, status = time.time() + 20, None
            while time.time() < deadline:
                status = self.store.task_status(task_id)
                if status in TERMINAL:
                    break
                time.sleep(0.02)
            else:
                self.fail(f"queued task never reached a terminal state: {status}")
        else:
            task = service.execute_inline(task_id)
            self.assertIsNotNone(task, "an inline run must return the finished task")
            self.assertIn(self.store.task_status(task_id), TERMINAL,
                          "the response was already sent, so the row cannot still be running")
        return self.store.get_task(task_id)

    def semantic_view(self, task):
        """Everything a visitor or the UI can depend on, minus timing and row ids."""
        events = [{"type": item["type"],
                   "payload": {k: v for k, v in item["payload"].items()
                               if k not in VOLATILE_EVENT_KEYS}}
                  for item in self.store.events_after(task["id"], 0, limit=200)]
        return {
            "plan": task["plan"],
            "steps": [{key: step[key] for key in ("idx", "title", "status", "detail", "output")}
                      for step in task["steps"]],
            "calls": [{key: call[key] for key in
                       ("tool", "status", "args", "result", "approval_required", "error")}
                      for call in task["calls"]],
            "artifacts": [{k: v for k, v in item.items() if k not in VOLATILE_ARTIFACT_KEYS}
                          for item in task["artifacts"]],
            "events": events,
            "status": task["status"], "report": task["report"],
            "error": task["error"], "error_code": task["error_code"],
            "provider": task["provider"], "model": task["model"],
            "ai_calls": task["ai_calls"], "tool_calls": task["tool_calls"],
            "usage": task["usage"], "pending_call": task["pending_call"], "active": task["active"],
        }


# A task that fits the tightest mode: 1 step, 1 tool call, 4 provider turns (plan, act,
# answer, report) -- exactly the inline ceiling. The equivalence tests use this script,
# because "same semantics" can only be proven about a task both schedules are allowed to
# finish. `SCRIPT` above is the *different-length* case: it proves the caps bind.
SMALL_SCRIPT = [plan(["احسب المتوسط"]),
                action("calculator", expression="(12+18+24)/3"),
                final("المتوسط 18"),
                "المتوسط 18، وحُسب في خطوة واحدة."]


class ModeResolution(Case):
    """Scenarios 1-3: the host decides the schedule, and only the schedule."""

    def test_standalone_render_is_queued(self):
        policy = self.policy()
        self.assertEqual((policy.mode, policy.serverless), (execution.QUEUED, False))
        self.assertTrue(policy.allows_approvals)
        self.assertFalse(policy.uses_request_visitor_token)
        self.assertIs(policy.config, Conf, "queued gets the public config untouched")
        self.assertEqual(policy.describe()["caps_applied"], {})
        task = self.run_task(policy)
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["ai_calls"], 6, "plan + two turns per step + report")

    def test_serverless_vercel_is_inline(self):
        policy = self.policy(serverless=True)
        self.assertEqual(policy.mode, execution.INLINE)
        self.assertFalse(policy.allows_approvals)
        self.assertFalse(policy.uses_request_visitor_token,
                         "a gateway token has no audience on this host")
        self.assertEqual((policy.config.MAX_STEPS, policy.config.DEADLINE_SECONDS,
                          policy.config.PROVIDER_TIMEOUT_SECONDS), (1, 45, 25))
        task = self.run_task(policy)
        # One step, so the second scripted action never runs; the point is the terminal
        # state arrives inside the request, not that the task is as rich as Render's.
        self.assertIn(task["status"], TERMINAL)
        self.assertEqual(len(task["steps"]), 1)
        self.assertFalse(policy.config is Conf)

    def test_promptql_gateway_is_inline_with_a_request_token(self):
        policy = self.policy(promptql=True)
        self.assertEqual(policy.mode, execution.INLINE)
        self.assertTrue(policy.uses_request_visitor_token)
        self.assertEqual(policy.config.MAX_STEPS, 2, "the gateway has no 45s freeze")
        self.assertEqual(policy.config.DEADLINE_SECONDS, 180)
        task = self.run_task(policy)
        self.assertEqual(len(task["steps"]), 2)

    def test_resolve_is_pure_and_validated(self):
        with self.assertRaises(execution.ExecutionPolicyError):
            execution.ExecutionPolicy("serverless")
        with self.assertRaises(execution.ExecutionPolicyError):
            execution.caps_for(execution.QUEUED, serverless=True)
        self.assertEqual(self.policy(serverless=True), self.policy(serverless=True))
        self.assertNotEqual(self.policy(), self.policy(promptql=True))
        # Caps clamp; they never widen. A base already below a cap stays as configured.
        capped = self.policy(serverless=True, config=Tight)
        self.assertEqual(capped.config.MAX_AI_CALLS, 1)
        self.assertEqual(capped.describe()["caps_applied"],
                         {"DEADLINE_SECONDS": [180, 45], "MAX_STEPS": [4, 1],
                          "PROVIDER_TIMEOUT_SECONDS": [75, 25]},
                         "MAX_AI_CALLS is the one cap that must not apply: the operator "
                         "already set 1, and a cap may only tighten, never restore 3")
        self.assertEqual(self.policy(config=Tight).describe()["budget"]["max_ai_calls"], 1)

    def test_no_serverless_knobs_were_invented(self):
        """The ratified rule: inline budgets ride on the knobs that already exist."""
        self.assertEqual([name for name in dir(Conf) if name.startswith("AGENT_SERVERLESS")], [])
        config_source = (ROOT / "backend" / "agent" / "config.py").read_text(encoding="utf-8")
        self.assertNotIn("SERVERLESS", config_source,
                         "a SERVERLESS_* knob appeared; the policy layer owns that number")
        self.assertEqual(self.policy(serverless=True).describe()["new_knobs"], 0)


class BudgetsAndFailures(Case):
    """Scenarios 4-6."""

    def approval_tool(self):
        def handler(ctx, args):
            self.ran.append("approved_tool")
            return {"ok": True}

        return Tool("approved_tool", "أداة تحتاج موافقة",
                    {"why": {"required": True, "type": "string"}}, handler,
                    requires_approval=True, read_only=False)

    def test_approval_tool_is_refused_inline_and_asked_queued(self):
        self.tools = Registry([self.approval_tool()])
        script = [plan(["نفّذ"]), action("approved_tool", why="لأن الطلب يقول"), final("لن يُقرأ")]

        asked = {}

        def wait_for_approval(call_id, timeout):
            asked["row"] = self.store.get_call(call_id)
            self.store.decide_call(call_id, False)      # as the HTTP approve route would
            return "denied"

        queued = self.run_task(self.policy(), script, hooks={"wait_for_approval": wait_for_approval})
        inline = self.run_task(self.policy(serverless=True), script)

        inline_calls = inline["calls"]
        self.assertEqual([call["status"] for call in inline_calls], ["rejected"])
        self.assertIn("موافقة", inline_calls[0]["error"])
        queued_calls = [call for call in queued["calls"] if call["tool"] == "approved_tool"]
        self.assertEqual([call["status"] for call in queued_calls], ["denied"])
        # Only the inline row carries a reason string. That asymmetry is the contract, not
        # a drift: a refusal with no human involved must be explained, while a denied
        # approval is already explained by the decision itself -- the loop hands that
        # reason to the model as the observation instead.
        self.assertIsNone(queued_calls[0]["error"], "the denial is a decision, not an error")
        self.assertTrue(asked["row"]["approval_required"], "the human was asked first")
        self.assertEqual(self.ran, [], "a refused or denied tool never executes, in either mode")

    def test_queueing_a_task_that_nobody_would_run_is_refused(self):
        policy = self.policy(serverless=True)
        service = Service(self.deps(policy), mode=policy.mode)
        with self.assertRaises(RuntimeError) as caught:
            service.submit("task-orphan")
        self.assertIn("inside the request", str(caught.exception))
        service.start()
        self.assertFalse(service.describe()["workers_started"],
                         "an inline deployment must not spawn a worker pool")
        self.assertEqual(service.queue_size(), 0)

    def test_budget_exhaustion_is_a_clear_failure_in_both_modes(self):
        script = [plan(["خطوة", "أخرى"]), action("calculator", expression="1+1"), final("لن يُقرأ")]
        for policy in (self.policy(config=Tight), self.policy(serverless=True, config=Tight)):
            task = self.run_task(policy, script)
            self.assertEqual(task["status"], "failed", policy.mode)
            self.assertEqual(task["error_code"], "ai_budget", policy.mode)
            self.assertTrue(task["error"].strip(), policy.mode)
            self.assertFalse(task["active"], policy.mode)
            self.assertNotIn(task["status"], ("queued", "running", "awaiting_approval"))

    def test_provider_rate_limit_fails_without_switching_provider(self):
        script = [ProviderError("بلغت الحد.", 429, "ai_rate_limit", 42)]
        for policy in (self.policy(), self.policy(serverless=True)):
            seen = []
            task = self.run_task(policy, script, hooks={
                "record_cooldown": lambda provider, user_id, retry: seen.append((provider, retry)),
                "wait_for_approval": lambda call_id, timeout: "denied"})
            self.assertEqual(task["status"], "failed", policy.mode)
            self.assertEqual(task["error_code"], "ai_rate_limit", policy.mode)
            self.assertEqual(task["provider"], "gemini", "no silent fallback to another host")
            self.assertEqual(seen, [("gemini", 42)], "the cooldown is mirrored once")
            self.assertEqual(self.provider_calls["n"], 1,
                             "one provider per task: chosen once, never re-picked")

    def test_inline_survives_a_crash_without_leaving_a_running_task(self):
        policy = self.policy(serverless=True)
        task_id = self.start_task()

        class Exploding:
            def run(self, _task_id):
                raise KeyboardInterrupt("worker died")

        service = Service(self.deps(policy), agent=Exploding(), mode=policy.mode)
        task = service.execute_inline(task_id)
        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["error_code"], "worker_error")
        self.assertNotEqual(self.store.task_status(task_id), "running",
                            "a dead thread must not strand a spinner")


class ContractEquivalence(Case):
    """Scenarios 7-8, and the test this whole phase exists to write."""

    def test_queued_and_inline_produce_identical_task_semantics(self):
        hooks = {"wait_for_approval": lambda call_id, timeout: "approved"}
        queued = self.semantic_view(self.run_task(self.policy(), SMALL_SCRIPT, hooks))
        inline = self.semantic_view(
            self.run_task(self.policy(promptql=True), SMALL_SCRIPT, hooks))
        self.maxDiff = None
        self.assertEqual(queued, inline)
        self.assertEqual([step["title"] for step in queued["plan"]],
                         [step["title"] for step in inline["plan"]],
                         "the same plan, in the same order, on both hosts")
        self.assertEqual([step["title"] for step in queued["plan"]], ["احسب المتوسط"])
        self.assertEqual(queued["status"], "completed")
        self.assertEqual({call["tool"] for call in queued["calls"]}, {"calculator"})
        self.assertTrue(all(call["status"] == "done" for call in queued["calls"]))
        self.assertEqual(queued["ai_calls"], 4, "the same four turns were billed in both modes")
        self.assertEqual(queued["tool_calls"], 1)
        self.assertEqual(queued["usage"], inline["usage"])

    def test_task_schema_keys_do_not_differ_by_mode(self):
        queued = self.run_task(self.policy(), SMALL_SCRIPT)
        for policy in (self.policy(serverless=True), self.policy(promptql=True)):
            self.assertEqual(sorted(self.run_task(policy, SMALL_SCRIPT)), sorted(queued),
                             f"{policy.mode} changed the task payload shape")
        # The field list is the published task contract; `events` is a separate endpoint
        # in both modes, so it is not expected here.
        for field in ("id", "goal", "status", "plan", "steps", "calls", "artifacts", "report",
                      "error", "error_code", "ai_calls", "tool_calls", "provider", "model",
                      "usage", "seconds_left", "pending_call", "active"):
            self.assertIn(field, queued, f"{field} vanished from a mode's payload")

    def test_event_names_are_the_same_in_both_modes(self):
        names = {}
        for label, policy in (("queued", self.policy()), ("inline", self.policy(promptql=True))):
            task = self.run_task(policy, SMALL_SCRIPT)
            names[label] = [event["type"] for event in self.store.events_after(task["id"], 0)]
        self.assertEqual(names["queued"], names["inline"],
                         "an SSE consumer must not need to know which host served the task")
        self.assertIn("task.plan", names["queued"])
        self.assertTrue(any(name.startswith("tool.") for name in names["queued"]),
                        "tool lifecycle is emitted in both modes, not only in the queue")


class ApprovalHandshake(unittest.TestCase):
    def test_decide_still_wakes_the_waiting_worker(self):
        # The handshake is not a mode concern; prove the refactor kept it working.
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = sqlite_store(str(Path(temp.name) / "handshake.db"))
        service = Service(AgentDeps(store=store, config=Conf, tools=build_registry(),
                                    provider_factory=lambda task=None: None, skills=[]))
        task_id = store.create_task("u_h", "هدف اختباري بطول كافٍ", "gemini", "fake",
                                    time.time() + 60)
        call_id = store.create_call(task_id, None, "approved_tool", {"why": "س"}, True)
        outcome = {}

        def waiter():
            outcome["decision"] = service.wait_for_approval(call_id, 5)

        thread = threading.Thread(target=waiter)
        thread.start()
        time.sleep(0.2)
        self.assertTrue(service.decide(call_id, True))
        thread.join(5)
        self.assertEqual(outcome["decision"], "approved")
        self.assertEqual(store.get_call(call_id)["status"], "approved")


if __name__ == "__main__":
    unittest.main()
