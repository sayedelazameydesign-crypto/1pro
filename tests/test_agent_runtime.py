"""End-to-end agent runtime tests: real Flask routes, real SQL, fake model.

The provider is the only stubbed piece, so plan -> tool call -> approval ->
report, the shared hourly budget, ownership and the CSRF/origin gates are all
exercised for real. Nothing here touches the network.
"""
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
temp = tempfile.TemporaryDirectory()
os.environ.update({
    "WAHA_DB": str(Path(temp.name) / "agent-test.db"),
    "WAHA_TRUST_PROMPTQL": "1",
    "WAHA_ALLOWED_ORIGINS": "https://pages.test",
    "GEMINI_API_KEY": "test-key-not-a-secret",
    "AGENT_NETWORK_TOOLS": "1",
    "AGENT_APPROVAL_TIMEOUT_SECONDS": "60",
    "AGENT_MAX_STEPS": "3",
    "AGENT_EVENT_RETENTION_SECONDS": "3600",
})
sys.path.insert(0, str(ROOT / "backend"))
spec = importlib.util.spec_from_file_location("waha_agent_backend", ROOT / "backend/app.py")
backend = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backend)
# The module reads its env at import; hand the process back clean so sibling
# test modules keep their own assumptions (no ambient AI key).
for _key in ("GEMINI_API_KEY", "AGENT_NETWORK_TOOLS", "AGENT_APPROVAL_TIMEOUT_SECONDS",
             "AGENT_MAX_STEPS", "AGENT_EVENT_RETENTION_SECONDS", "WAHA_DB"):
    os.environ.pop(_key, None)

from agent import execution as agent_execution    # noqa: E402
from agent.providers import FakeProvider, Result  # noqa: E402

ORIGIN = "https://pages.test"


def identity(user):
    import base64
    value = base64.urlsafe_b64encode(json.dumps({"sub": user, "exp": time.time() + 600}).encode())
    return "test." + value.decode().rstrip("=") + ".test"


def plan(titles):
    return {"steps": [{"title": title, "goal": title + " بالتفصيل"} for title in titles]}


def action(tool, **args):
    return {"thought": "أحتاج الأداة", "action": {"tool": tool, "args": args}, "final": None}


def final(text):
    return {"thought": "كفى", "action": None, "final": text}


class AgentCase(unittest.TestCase):
    def setUp(self):
        # `AGENT_NETWORK_TOOLS=1` above is only honoured if this file is the first module
        # to import agent.config, because AgentConfig reads the environment once at import
        # time -- and which sibling module gets imported first is an accident of test
        # discovery. Patch the attribute instead, so "web_fetch exists" is a property of
        # this test, not of the run order. (The same trap is why test_agent_execution
        # passes its own config class rather than trusting the ambient one.)
        network = patch.object(backend.AgentConfig, "NETWORK_TOOLS", True)
        network.start()
        self.addCleanup(network.stop)
        self.client = backend.app.test_client()
        self.user = "u_" + os.urandom(10).hex()
        self.fake = None
        self.provider_patch = None

    # -- helpers --------------------------------------------------------------
    def headers(self):
        return {"Origin": ORIGIN, "X-PromptQL-Visitor-Token": identity(self.user),
                "X-Waha-CSRF": backend.csrf_for(self.user), "Content-Type": "application/json"}

    def use_script(self, script):
        self.fake = FakeProvider(script=list(script))
        self.provider_patch = patch.object(backend, "agent_provider",
                                          lambda task=None, visitor_token=None, timeout=None: self.fake)
        self.provider_patch.start()
        self.addCleanup(self.provider_patch.stop)

    def wait(self, task_id, wanted=("completed", "failed", "cancelled", "interrupted", "expired"),
             timeout=8.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            task = backend.agent_store.get_task(task_id)
            if task and task["status"] in wanted:
                return task
            time.sleep(0.05)
        self.fail(f"task {task_id} never reached a terminal state: {task['status'] if task else None}")

    def create(self, goal="علّمني حساب متوسط أرقام، خطوة واحدة", **extra):
        response = self.client.post("/api/agent/tasks",
                                    data=json.dumps({"goal": goal, **extra}), headers=self.headers())
        return response

    def test_goal_validation_and_auth(self):
        short = self.client.post("/api/agent/tasks", data=json.dumps({"goal": "قصير"}),
                                 headers=self.headers())
        self.assertEqual(short.status_code, 400)
        no_csrf = dict(self.headers())
        no_csrf.pop("X-Waha-CSRF")
        self.assertEqual(self.client.post("/api/agent/tasks",
                                          data=json.dumps({"goal": "هدف طويل بما يكفي"}),
                                          headers=no_csrf).status_code, 403)
        self.assertEqual(self.client.post("/api/agent/tasks",
                                          data=json.dumps({"goal": "هدف طويل بما يكفي"}),
                                          headers={"Origin": "https://evil.invalid"}).status_code, 403)

    def test_task_runs_plan_tool_and_report(self):
        self.use_script([plan(["احسب المتوسط"]), action("calculator", expression="(2+4+6)/3"),
                         final("المتوسط يساوي 4"), final("أنجزنا الحساب")])
        created = self.create()
        self.assertEqual(created.status_code, 201)
        body = created.get_json()
        self.assertEqual(body["mode"], "queued")
        task = self.wait(body["task"]["id"])
        self.assertEqual(task["status"], "completed")
        self.assertEqual(len(task["steps"]), 1)
        self.assertEqual(task["steps"][0]["status"], "done")
        self.assertIn("4", task["steps"][0]["output"])
        self.assertIn("أنجزنا الحساب", task["report"])
        self.assertEqual(task["tool_calls"], 1)
        self.assertEqual(task["usage"]["prompt_tokens"], 40)
        self.assertEqual([call["tool"] for call in task["calls"]], ["calculator"])
        self.assertEqual(task["calls"][0]["status"], "done")
        self.assertEqual(task["calls"][0]["result"]["result"], 4)

    def test_kb_search_evidence_reaches_the_next_turn_and_the_report(self):
        """R3's acceptance gate, end to end: Agent -> kb_search -> rag_search -> R1.

        The citation must be visible in three places at once -- the persisted tool
        result, the message handed back to the model, and the report -- and identical in
        all three. Anything weaker would let a layer re-word the evidence.
        """
        import rag_search
        expected = rag_search.search("كيف أحدد جمهور الرسالة", k=5)
        self.assertTrue(expected["results"], "the shipped sample index must answer this")
        citation = expected["results"][0]["citation"]

        self.use_script([plan(["ابحث في المكتبة"]),
                         action("kb_search", query="كيف أحدد جمهور الرسالة"),
                         final(f"استنادًا إلى {citation}: ابدأ بالجمهور ثم الصياغة."),
                         final(f"التقرير يعتمد على {citation}")])
        created = self.create(goal="ساعدني أحدد جمهور رسالتي قبل الكتابة")
        self.assertEqual(created.status_code, 201)
        task = self.wait(created.get_json()["task"]["id"])
        self.assertEqual(task["status"], "completed")
        self.assertEqual([call["tool"] for call in task["calls"]], ["kb_search"])
        call = task["calls"][0]
        self.assertEqual(call["status"], "done")
        self.assertFalse(call["approval_required"], "read-only retrieval must not pause the task")
        self.assertIsNone(task["pending_call"], "nothing may wait for a human on a search")
        result = call["result"]
        self.assertEqual(result["evidence"], "RAG_LOCAL")
        self.assertEqual(result["results"][0]["citation"], citation)
        self.assertEqual(result["results"][0]["chunk_id"], expected["results"][0]["chunk_id"])
        # ctx.rag_index_dir was injected by the app, so the agent read the repo's own
        # index -- the same chunk count the HTTP endpoint reports, not a private copy.
        self.assertEqual(result["corpus"]["chunks"], expected["index"]["chunks"])
        self.assertEqual(result["corpus"]["chunks"], 18)
        self.assertEqual(result["no_answer"]["decision"], "deferred")
        self.assertNotIn("answer", result)
        # The observation reached the next model turn verbatim, with its citation.
        # Asserted by the citation and by the observation's own key -- the data and
        # the shape it travelled in -- not by the label the loop prints in front of it.
        # What the model must receive, read from the message content itself: the
        # citation, and the observation's own key. (Through `json.dumps` the inner
        # quotes arrive escaped, so a dump is the wrong thing to search -- and the
        # label the loop prints in front of the observation is copy, not contract.)
        contents = [message.get("content", "") for item in self.fake.calls
                    for message in item["messages"]
                    if isinstance(message.get("content"), str)]
        self.assertTrue(any(citation in text and '"results"' in text for text in contents),
                        "the tool result must be fed back to the model, not swallowed")
        self.assertIn(citation, task["report"])

    def test_kb_search_reports_an_unavailable_library_as_an_error(self):
        """Gate 5 inside the loop: infrastructure failure must not look like "no results"."""
        import rag_search as rag_module
        self.use_script([plan(["ابحث في المكتبة"]),
                         action("kb_search", query="بريد"),
                         final("المكتبة غير متاحة الآن، فأجبت من معرفتي دون استشهاد."),
                         final("أُبلغ بعدم توفّر المكتبة")])
        with patch.object(rag_module, "search",
                          side_effect=rag_module.RagSearchUnavailable("index.json missing")):
            created = self.create(goal="علّمني كتابة بريد مهني قصير")
            task = self.wait(created.get_json()["task"]["id"])
        self.assertEqual(task["status"], "completed", "a tool error must not kill the task")
        call = task["calls"][0]
        self.assertEqual(call["status"], "error")
        self.assertNotIn("results", call["error"])          # no invented hits
        # The operator-facing cause must survive the trip, asserted by the identifier
        # the message carries (`rag_index`) rather than by a sentence that can be
        # reworded without changing behaviour.
        # The cause the retrieval layer raised is relayed verbatim ("index.json
        # missing" is the stub's own text, not our copy): a summarised cause is how
        # an operator loses the one detail that identifies the failure.
        self.assertIn("index.json", call["error"])
        # What the model must receive is a failure it can act on: the observation's
        # `error` field, carrying the cause the retrieval layer relayed ("index.json",
        # the stub's own text). Matching the Arabic message instead would break the day
        # the message is reworded -- which is a language edit, not a regression.
        # What the model must receive is a failure it can act on: the observation's
        # `error` field carrying the cause the retrieval layer relayed ("index.json",
        # the stub's own text). Matching the Arabic message instead would break the
        # day it is reworded -- a language edit, not a regression.
        contents = [message.get("content", "") for item in self.fake.calls
                    for message in item["messages"]
                    if isinstance(message.get("content"), str)]
        self.assertTrue(any('"error"' in text and "index.json" in text for text in contents),
                        "the model must be told the tool failed, with its cause")

    def test_tool_results_are_persisted_as_events(self):
        self.use_script([plan(["اضبط تاريخاً"]), action("clock", offset_days=1),
                         final("تم"), final("تقرير")])
        task_id = self.create().get_json()["task"]["id"]
        self.wait(task_id)
        events = backend.agent_store.events_after(task_id, 0)
        types = [event["type"] for event in events]
        for expected in ("task.created", "task.plan", "step.queued", "step.running",
                         "tool.done", "step.done", "task.completed"):
            self.assertIn(expected, types)
        self.assertTrue(all(event["payload"] is not None for event in events))

    def test_approval_denied_means_the_tool_never_runs(self):
        self.use_script([plan(["اجلب صفحة"]), action("web_fetch", url="https://example.invalid/x"),
                         final("لم أنفذ الأداة بسبب الرفض"), final("تقرير")])
        created = self.create()
        task_id = created.get_json()["task"]["id"]
        waiting = None
        deadline = time.time() + 8
        while time.time() < deadline:
            waiting = backend.agent_store.get_task(task_id)
            if waiting["status"] == "awaiting_approval" and waiting["pending_call"]:
                break
            time.sleep(0.05)
        self.assertIsNotNone(waiting)
        self.assertEqual(waiting["status"], "awaiting_approval")
        call_id = waiting["pending_call"]
        self.assertEqual([call["id"] for call in waiting["calls"]], [call_id])
        self.assertTrue(waiting["calls"][0]["approval_required"])
        decision = self.client.post(f"/api/agent/tasks/{task_id}/approve",
                                    data=json.dumps({"call_id": call_id, "approve": False}),
                                    headers=self.headers())
        self.assertEqual(decision.status_code, 200)
        task = self.wait(task_id)
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["calls"][0]["status"], "denied")
        self.assertIsNone(task["calls"][0]["result"])
        # The model is told why, checked as JSON: the payload has an `error` field
        # with something in it. Asserting the sentence would break on edits to copy
        # that no caller depends on.
        told = json.loads(task["steps"][0]["output"])
        self.assertIn("error", told)
        self.assertTrue(told["error"].strip())

    def test_code_exec_runs_behind_the_approval_gate_and_records_its_exit_status(self):
        """R7.3+R7.4, proven end to end instead of asserted.

        The tool reaches the loop with no change to `runtime.py`: it is registered
        in the registry, it waits on the same approval handshake as `web_fetch`,
        and its result is persisted in `agent_tool_calls`. The exit status has to
        survive that trip -- a run the store cannot tell apart from a success is
        how a crashed program reads as a working one.

        On a machine that cannot build the boundary the *refusal* is asserted
        instead: the same call must come back as a recorded error and never as a
        silent success. Both branches are exercised in CI runs of this file; which
        one this machine takes is decided by `sandbox.detect()` and printed by
        `tests/test_agent_sandbox.py`.
        """
        from agent import sandbox as sandbox_module
        code_exec = patch.object(backend.AgentConfig, "CODE_EXEC", True)
        code_exec.start()
        self.addCleanup(code_exec.stop)
        # The program the loop runs does two things: it answers, and it tries to
        # write into the project directory. The second half is the point -- the
        # host is checked afterwards, so a result that *says* the filesystem was
        # isolated while the file lands on disk cannot pass.
        victim = ROOT / "PWNED-BY-THE-LOOP.txt"
        program = ("import os\n"
                   "victim = %r\n"
                   "print('answer', 6 * 7)\n"
                   "print('host-visible', os.path.exists(victim))\n"
                   "try:\n"
                   "    open(victim, 'w').write('pwned')\n"
                   "    print('HOST-WRITE-SUCCEEDED')\n"
                   "except OSError as error:\n"
                   "    print('host-write-blocked', type(error).__name__)\n" % str(victim))
        self.use_script([plan(["احسب بـبايثون"]),
                         action("code_exec", code=program),
                         final("نفّذت الحساب"), final("تقرير")])
        task_id = self.create().get_json()["task"]["id"]
        waiting = None
        deadline = time.time() + 8
        while time.time() < deadline:
            waiting = backend.agent_store.get_task(task_id)
            if waiting["status"] == "awaiting_approval" and waiting["pending_call"]:
                break
            time.sleep(0.05)
        self.assertIsNotNone(waiting, "code_exec must pause for approval like any egress tool")
        call_id = waiting["pending_call"]
        self.assertEqual(waiting["calls"][0]["tool"], "code_exec")
        self.assertTrue(waiting["calls"][0]["approval_required"],
                        "code execution without an approval would bypass the loop's only gate")
        decision = self.client.post(f"/api/agent/tasks/{task_id}/approve",
                                    data=json.dumps({"call_id": call_id, "approve": True}),
                                    headers=self.headers())
        self.assertEqual(decision.status_code, 200)
        task = self.wait(task_id)
        call = task["calls"][0]
        if sandbox_module.detect().supports():
            self.assertEqual(call["status"], "done", call.get("error"))
            result = call["result"]
            self.assertEqual(result["exit_status"], 0)
            self.assertIn("42", result["stdout"])
            self.assertTrue(result["filesystem_isolated"],
                            "the boundary reports the guarantee the runner enforced")
            self.assertEqual(result["isolation"], sandbox_module.ISOLATION_PRIVATE_ROOT)
            # Behavioural, not declarative: the write was attempted through the
            # loop and the project directory is unchanged.
            self.assertIn("host-visible False", result["stdout"])
            self.assertIn("host-write-blocked", result["stdout"])
            self.assertNotIn("HOST-WRITE-SUCCEEDED", result["stdout"])
            self.assertFalse(victim.exists(), "code executed by the loop wrote into the repository")
        else:
            # A refusal is recorded as an error with no result, and the reason names
            # the capability by its identifier. Asserting the Arabic would tie the
            # suite to copy; asserting the constant ties it to the decision.
            self.assertEqual(call["status"], "error")
            self.assertIsNone(call["result"])
            self.assertIn(sandbox_module.CAPABILITY_FILESYSTEM_ISOLATION, call["error"])

    def test_approval_endpoint_rejects_stale_calls(self):
        self.use_script([plan(["احسب"]), action("calculator", expression="1+1"), final("2"), final("r")])
        task_id = self.create().get_json()["task"]["id"]
        self.wait(task_id)
        response = self.client.post(f"/api/agent/tasks/{task_id}/approve",
                                    data=json.dumps({"call_id": "tc_missing", "approve": True}),
                                    headers=self.headers())
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["code"], "approval_stale")

    def test_memory_tool_writes_visible_user_memory(self):
        self.use_script([plan(["احفظ تفضيلي"]),
                         action("memory_write", content="أفضل التمارين القصيرة", kind="preference"),
                         final("حُفظ"), final("تقرير")])
        task_id = self.create().get_json()["task"]["id"]
        self.wait(task_id)
        listing = self.client.get("/api/agent/memory",
                                  headers={"Origin": ORIGIN,
                                           "X-PromptQL-Visitor-Token": identity(self.user)})
        self.assertEqual(listing.status_code, 200)
        rows = listing.get_json()["memory"]
        self.assertEqual([row["content"] for row in rows], ["أفضل التمارين القصيرة"])
        self.assertEqual(rows[0]["kind"], "preference")
        removed = self.client.post(f"/api/agent/memory/{rows[0]['id']}/delete", data="{}",
                                   headers=self.headers())
        self.assertTrue(removed.get_json()["ok"])

    def test_memory_endpoints_need_no_ai(self):
        added = self.client.post("/api/agent/memory", data=json.dumps({"content": "ملاحظة اختبار",
                                                                      "kind": "goal"}),
                                 headers=self.headers())
        self.assertEqual(added.status_code, 201)
        self.assertEqual(added.get_json()["items"], 1)
        bad = self.client.post("/api/agent/memory", data=json.dumps({"content": "x", "kind": "note"}),
                               headers=self.headers())
        self.assertEqual(bad.status_code, 400)
        wrong_kind = self.client.post("/api/agent/memory",
                                      data=json.dumps({"content": "محتوى كافٍ", "kind": "orders"}),
                                      headers=self.headers())
        self.assertEqual(wrong_kind.status_code, 400)

    def test_artifact_created_by_agent_is_private_and_downloadable(self):
        self.use_script([plan(["ارفق صفحة"]),
                         action("artifact_write", name="index.html", kind="html",
                                content="<h1>مرحبا</h1>"),
                         final("أنشأت الملف"), final("تقرير")])
        task_id = self.create().get_json()["task"]["id"]
        task = self.wait(task_id)
        self.assertEqual(len(task["artifacts"]), 1)
        artifact_id = task["artifacts"][0]["id"]
        mine = self.client.get(f"/api/agent/artifacts/{artifact_id}",
                               headers={"Origin": ORIGIN,
                                        "X-PromptQL-Visitor-Token": identity(self.user)})
        self.assertEqual(mine.status_code, 200)
        self.assertIn("مرحبا", mine.get_json()["artifact"]["content"])
        other = "u_" + os.urandom(10).hex()
        theirs = self.client.get(f"/api/agent/artifacts/{artifact_id}",
                                 headers={"Origin": ORIGIN,
                                          "X-PromptQL-Visitor-Token": identity(other)})
        self.assertEqual(theirs.status_code, 404)

    def test_rejects_bad_artifact_requests_through_the_tool(self):
        self.use_script([plan(["ارفق ملفاً"]), action("artifact_write", name="../escape.html",
                                                      kind="html", content="<b>x</b>"),
                         action("artifact_write", name="evil.html", kind="html",
                                content="<script src=\"https://evil.invalid/a.js\"></script>"),
                         final("انتهى"), final("تقرير")])
        task_id = self.create().get_json()["task"]["id"]
        task = self.wait(task_id)
        self.assertEqual([call["status"] for call in task["calls"]], ["error", "error"])
        self.assertEqual(task["artifacts"], [], "a refused artifact must not appear")
        # Why each was refused is asserted where the decision lives -- the tool's own
        # `ToolError.code` in `tests/test_agent_core.py` -- and here only that the
        # loop recorded a reason for both, as data.
        for call in task["calls"]:
            self.assertTrue(call["error"].strip())

    def test_cancel_stops_a_running_task(self):
        class Slow:
            name = "slow"
            label = "Slow"
            streaming = False
            json_mode = True
            model = "slow-model"

            def __init__(self):
                self.calls = 0

            def complete(self, system, messages, max_output_tokens=1200, temperature=0.4,
                         json_mode=False):
                self.calls += 1
                time.sleep(0.2)
                if self.calls == 1:
                    payload = plan(["طويلة", "أطول", "الأطول"])
                else:
                    payload = final("خطوة " + str(self.calls))
                return Result(json.dumps(payload, ensure_ascii=False), {}, provider="slow")

        slow = Slow()
        with patch.object(backend, "agent_provider",
                        lambda task=None, visitor_token=None, timeout=None: slow):
            task_id = self.create().get_json()["task"]["id"]
            deadline = time.time() + 5
            while time.time() < deadline and backend.agent_store.task_status(task_id) == "queued":
                time.sleep(0.02)
            cancelled = self.client.post(f"/api/agent/tasks/{task_id}/cancel", data="{}",
                                        headers=self.headers())
            self.assertEqual(cancelled.status_code, 200)
            task = self.wait(task_id, wanted=("cancelled", "completed"))
            self.assertEqual(task["status"], "cancelled")
            self.assertLess(slow.calls, 12)
            self.assertEqual(backend.agent_store.task_status(task_id), "cancelled")

    def test_busy_guard_blocks_a_second_active_task(self):
        class Slow:
            name = "slow"
            label = "Slow"
            streaming = False
            json_mode = True
            model = "slow-model"

            def __init__(self):
                self.calls = 0

            def complete(self, system, messages, max_output_tokens=1200, temperature=0.4,
                         json_mode=False):
                self.calls += 1
                time.sleep(0.4)
                payload = plan(["خطوة واحدة"]) if self.calls == 1 else final("...")
                return Result(json.dumps(payload, ensure_ascii=False), {}, provider="slow")

        slow = Slow()
        with patch.object(backend, "agent_provider",
                        lambda task=None, visitor_token=None, timeout=None: slow):
            first = self.create().get_json()["task"]["id"]
            deadline = time.time() + 5
            while time.time() < deadline and backend.agent_store.task_status(first) != "running":
                time.sleep(0.02)
            second = self.create(goal="مهمة ثانية أثناء تشغيل الأولى")
            self.assertEqual(second.status_code, 409)
            self.assertEqual(second.get_json()["code"], "agent_busy")
            self.client.post(f"/api/agent/tasks/{first}/cancel", data="{}", headers=self.headers())
            self.wait(first, wanted=("cancelled", "completed"))

    def test_rate_limit_is_shared_with_the_chat_path(self):
        self.use_script([plan(["احسب"]), action("calculator", expression="1+1"), final("2"), final("r")])
        user_limit = backend.USER_AI_LIMIT_PER_HOUR
        uid = self.user
        with backend.connect() as db:
            for _ in range(user_limit):
                backend.run(db, "INSERT INTO attempts VALUES(?,?)", (uid, time.time()))
        response = self.create()
        self.assertEqual(response.status_code, 201)
        task = self.wait(response.get_json()["task"]["id"])
        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["error_code"], "local_rate_limit")
        self.assertTrue(task["error"].strip(), "a failed task must carry a readable reason")

    def test_provider_rate_limit_records_nvidia_cooldown(self):
        from agent.providers import ProviderError
        seen = {}

        def record(provider, user_id, retry_after):
            seen.update({"provider": provider, "user_id": user_id, "retry_after": retry_after})

        self.use_script([ProviderError("بلغت Gemini حد الطلبات.", 429, "ai_rate_limit", 42)])
        with patch.dict(backend.__dict__, {}), patch.object(backend, "agent_record_cooldown", record):
            # rebind the hook the service already captured, then run inline so no
            # background worker is involved in the assertion.
            task_id = backend.agent_store.create_task(self.user, "هدف يكفي لطول الطلب",
                                                       "gemini", "model", time.time() + 60)
            policy = agent_execution.ExecutionPolicy(agent_execution.INLINE,
                                                    config=backend.AgentConfig)
            deps = backend.agent_deps(policy=policy)
            deps["hooks"]["record_cooldown"] = record
            # R6: the request-bound path goes through the same Service the route uses,
            # so this asserts the real inline contract, not a hand-assembled Agent.
            task = backend.Service.for_request(backend.AgentDeps(**deps), policy).execute_inline(task_id)
        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["error_code"], "ai_rate_limit")
        self.assertEqual(seen["retry_after"], 42)
        self.assertEqual(seen["provider"], "gemini")

    def test_nvidia_is_refused_in_standalone_mode(self):
        response = self.create(provider="nvidia")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["code"], "nvidia_requires_gateway")

    def test_ai_disabled_blocks_task_creation(self):
        with patch.object(backend, "ai_mode", lambda: "disabled"):
            response = self.create()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["code"], "ai_disabled")

    def test_inline_mode_refuses_tools_that_need_approval(self):
        self.use_script([plan(["اجلب"]), action("web_fetch", url="https://example.invalid/a"),
                         final("أجبت بلا أدوات"), final("تقرير")])
        with patch.object(backend, "ai_mode", lambda: "promptql"):
            created = self.create()
            self.assertEqual(created.status_code, 201)
            body = created.get_json()
            self.assertEqual(body["mode"], "inline")
            task = body["task"]
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["calls"][0]["status"], "rejected")
        # `rejected` is the machine signal (distinct from `denied` and `error`); the
        # reason beside it has to exist and be readable, not match a sentence.
        self.assertTrue(task["calls"][0]["error"].strip())
        self.assertEqual(task["plan"][0]["title"], "اجلب")

    def test_serverless_runs_inline_before_the_platform_freezes(self):
        self.use_script([plan(["احسب"]), action("calculator", expression="8*8"),
                         final("64"), final("لن يُقرأ لأن التقرير يعود من الخطوات")])
        with patch.dict(os.environ, {"VERCEL": "1"}):
            config = self.client.get("/api/agent/config", headers={"Origin": ORIGIN}).get_json()
            self.assertTrue(config["inline"])
            created = self.create()
            self.assertEqual(created.status_code, 201)
            body = created.get_json()
        self.assertEqual(body["mode"], "inline")
        task = body["task"]
        self.assertEqual(task["status"], "completed")
        self.assertIn("64", task["steps"][0]["output"])
        self.assertLessEqual(task["seconds_left"], 45)
        # 3 calls fit the serverless budget (plan + 2 turns); the summary call is
        # refused and the report falls back to the step digest instead of failing.
        self.assertEqual(task["ai_calls"], 3)
        # The fallback digest carries each step's own title and status, read from the
        # task rather than retyped: the claim is "the step's data reached the report",
        # and no word of it can coincide with the tool copy by accident.
        step = task["steps"][0]
        self.assertIn(step["title"], task["report"])
        self.assertIn(f"[{step['status']}]", task["report"])

    def test_config_and_me_advertise_the_runtime(self):
        config = self.client.get("/api/agent/config", headers={"Origin": ORIGIN}).get_json()
        self.assertTrue(config["enabled"])
        self.assertEqual(config["limits"]["max_steps"], backend.AgentConfig.MAX_STEPS,
                         "the advertised ceiling must be the enforced one, whatever the "
                         "environment froze at import")
        names = [tool["name"] for tool in config["tools"]]
        self.assertIn("web_fetch", names)
        self.assertTrue(next(tool for tool in config["tools"] if tool["name"] == "web_fetch")["requires_approval"])
        me = self.client.get("/api/me", headers={"Origin": ORIGIN,
                                                "X-PromptQL-Visitor-Token": identity(self.user)}).get_json()
        self.assertTrue(me["agent"]["enabled"])
        self.assertEqual(me["agent"]["model"], "gemini-3.1-flash-lite")

    def test_sse_stream_replays_finished_task(self):
        self.use_script([plan(["احسب"]), action("calculator", expression="6*7"), final("42"), final("r")])
        task_id = self.create().get_json()["task"]["id"]
        self.wait(task_id)
        response = self.client.get(f"/api/agent/tasks/{task_id}/stream",
                                  headers={"Origin": ORIGIN,
                                           "X-PromptQL-Visitor-Token": identity(self.user)})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/event-stream")
        body = response.get_data(as_text=True)
        self.assertIn("event: task.created", body)
        self.assertIn("event: tool.done", body)
        self.assertIn("event: close", body)

    def test_unknown_or_foreign_task_is_404(self):
        headers = self.headers()
        self.assertEqual(self.client.get("/api/agent/tasks/nope", headers=headers).status_code, 404)
        self.assertEqual(self.client.post("/api/agent/tasks/nope/cancel", data="{}",
                                         headers=headers).status_code, 404)

    def test_tasks_are_scoped_per_visitor(self):
        self.use_script([plan(["احسب"]), action("calculator", expression="3*3"), final("9"), final("r")])
        task_id = self.create().get_json()["task"]["id"]
        self.wait(task_id)
        other = "u_" + os.urandom(10).hex()
        mine = self.client.get("/api/agent/tasks", headers={"Origin": ORIGIN,
                                                           "X-PromptQL-Visitor-Token": identity(self.user)})
        theirs = self.client.get("/api/agent/tasks", headers={"Origin": ORIGIN,
                                                             "X-PromptQL-Visitor-Token": identity(other)})
        self.assertEqual([task["id"] for task in mine.get_json()["tasks"]], [task_id])
        self.assertEqual(theirs.get_json()["tasks"], [])
        self.assertEqual(self.client.get(f"/api/agent/tasks/{task_id}",
                                        headers={"Origin": ORIGIN,
                                                 "X-PromptQL-Visitor-Token": identity(other)}).status_code, 404)

    def test_delete_removes_task_and_children(self):
        self.use_script([plan(["احسب"]), action("calculator", expression="1+2"), final("3"), final("r")])
        task_id = self.create().get_json()["task"]["id"]
        self.wait(task_id)
        removed = self.client.post(f"/api/agent/tasks/{task_id}/delete", data="{}", headers=self.headers())
        self.assertTrue(removed.get_json()["ok"])
        with backend.connect() as db:
            own = backend.run(db, "SELECT COUNT(1) AS n FROM agent_tasks WHERE id=?",
                              (task_id,)).fetchone()["n"]
            self.assertEqual(own, 0)
            for table in ("agent_steps", "agent_events", "agent_tool_calls", "agent_artifacts"):
                count = backend.run(db, f"SELECT COUNT(1) AS n FROM {table} WHERE task_id=?",
                                    (task_id,)).fetchone()["n"]
                self.assertEqual(count, 0, table)

    def test_interrupted_tasks_are_repaired_on_boot(self):
        task_id = backend.agent_store.create_task("u_bootuser", "هدف يكفي الطول تماماً", "gemini",
                                                 "model", time.time() + 60)
        self.assertEqual(backend.agent_store.task_status(task_id), "queued")
        recovered = backend.agent_store.recover_interrupted()
        self.assertGreaterEqual(recovered, 1)
        self.assertEqual(backend.agent_store.task_status(task_id), "interrupted")
        self.assertEqual(backend.agent_store.get_task(task_id)["error_code"], "interrupted")


if __name__ == "__main__":
    unittest.main()
