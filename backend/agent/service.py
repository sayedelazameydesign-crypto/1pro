"""Task service: the queue, the worker threads and the approval handshake.

The runtime itself is pure (see `runtime.py`); this module owns the messy parts:
* one small worker pool (default 1) because the free Render instance has 0.1 CPU
  and one shared Gemini quota - parallel agents only make everyone slower;
* the approval handshake: the worker blocks on a threading.Event, the HTTP
  request that carries the human decision sets it. If the process dies first,
  `recover_interrupted()` at boot is the safety net;
* `execute_inline()` for deployments that must not background (PromptQL mode,
  where the visitor's gateway token only exists inside the request), and for
  serverless platforms whose request must produce the result itself.
"""
import logging
import queue
import threading
import time

logger = logging.getLogger("waha.agent")

TERMINAL = ("completed", "failed", "cancelled", "interrupted", "expired")


class Service:
    def __init__(self, deps, agent=None):
        from .runtime import Agent
        self.deps = deps
        self.agent = agent or Agent(deps)
        self._queue = queue.Queue()
        self._workers = []
        self._approvals = {}
        self._lock = threading.Lock()
        self._started = False

    # -- worker pool ----------------------------------------------------------
    def start(self):
        with self._lock:
            if self._started:
                return
            self._started = True
            for index in range(self.deps.config.WORKERS):
                worker = threading.Thread(target=self._work, name=f"waha-agent-{index}", daemon=True)
                self._workers.append(worker)
                worker.start()

    def _work(self):
        while True:
            task_id = self._queue.get()
            try:
                self.agent.run(task_id)
            except BaseException:  # noqa: BLE001 - the worker must survive any single task
                # A silent worker death is the worst failure mode here: the task
                # would stay "running" forever and the visitor would only see a
                # spinner. Log, mark the task failed, keep serving the queue.
                logger.exception("agent task %s crashed outside the runtime", task_id)
                try:
                    self.deps.store.finish(task_id, "failed",
                                           error="تعذّر إكمال المهمة بسبب خطأ في الخادم.",
                                           error_code="worker_error")
                except Exception:  # noqa: BLE001
                    logger.exception("could not record the failure for task %s", task_id)
            finally:
                self._queue.task_done()

    def submit(self, task_id):
        self.start()
        self._queue.put(task_id)
        return True

    def execute_inline(self, task_id):
        """Run in the calling thread (promptql mode / serverless / tests)."""
        return self.agent.run(task_id)

    def queue_size(self):
        return self._queue.qsize()

    # -- approvals ------------------------------------------------------------
    def wait_for_approval(self, call_id, timeout):
        """Called from a worker. Returns approved/denied/cancelled/timeout."""
        event = threading.Event()
        with self._lock:
            self._approvals[call_id] = {"event": event, "decision": None}
        store = self.deps.store
        deadline = time.time() + timeout
        while not event.wait(0.5):
            row = store.get_call(call_id)
            if row is None:
                break
            if row["status"] in ("approved", "denied", "expired"):
                break
            if time.time() > deadline:
                break
        with self._lock:
            record = self._approvals.pop(call_id, None) or {}
        decision = record.get("decision")
        if decision:
            return decision
        row = store.get_call(call_id)
        status = (row or {}).get("status")
        if status == "approved":
            return "approved"
        if status == "denied":
            return "denied"
        store.complete_call(call_id, error="انتهت مهلة الموافقة، ولم تُنفَّذ الأداة.", status="expired")
        return "timeout"

    def decide(self, call_id, approve):
        """Called from the HTTP request carrying the human answer."""
        if not self.deps.store.decide_call(call_id, approve):
            return False
        with self._lock:
            record = self._approvals.get(call_id)
            if record is not None:
                record["decision"] = "approved" if approve else "denied"
                record["event"].set()
        return True

    def cancel(self, task_id):
        """Flip the DB flag the loop checks, then wake anyone still waiting."""
        cancelled = self.deps.store.request_cancel(task_id)
        with self._lock:
            records = list(self._approvals.values())
        for record in records:
            record["decision"] = "cancelled"
            record["event"].set()
        return cancelled

    # -- boot/teardown --------------------------------------------------------
    def bootstrap(self):
        self.deps.store.apply_schema()
        recovered = self.deps.store.recover_interrupted()
        retention = getattr(self.deps.config, "EVENT_RETENTION_SECONDS", 86400)
        purged = self.deps.store.purge_old(retention)
        if self.deps.config.WORKERS:
            self.start()
        return {"recovered": recovered, "purged_tasks": purged, "workers": self.deps.config.WORKERS}
