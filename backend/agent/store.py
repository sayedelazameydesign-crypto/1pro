"""Persistence for the agent runtime.

One place that knows SQL for the agent, written against the same portable
conventions as the rest of the backend: `?` placeholders, SQLite-style upserts,
and a tiny adapter (`connect`, `run`, `insert_returning_id`, `postgres`) handed in
by `app.py`. That keeps this module importable in tests without a Flask request
or a Postgres server.

Everything lives in the database rather than in memory: Render's free instance
sleeps after 15 idle minutes and Vercel recycles containers, so a task must be
resumable-looking (and truthfully marked `interrupted`) across restarts.
"""
import json
import time
import uuid

TERMINAL_STATUSES = ("completed", "failed", "cancelled", "interrupted", "expired")
ACTIVE_STATUSES = ("queued", "running", "awaiting_approval")

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_tasks(
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, goal TEXT NOT NULL, status TEXT NOT NULL,
    provider TEXT NOT NULL, model TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
    finished_at REAL, deadline_at REAL NOT NULL, plan TEXT, report TEXT, error TEXT,
    error_code TEXT, pending_call TEXT, ai_calls INTEGER NOT NULL DEFAULT 0,
    tool_calls INTEGER NOT NULL DEFAULT 0, prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0, ip_key TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS agent_tasks_user ON agent_tasks(user_id, updated_at);
CREATE TABLE IF NOT EXISTS agent_steps(
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL
        REFERENCES agent_tasks(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL, title TEXT NOT NULL, status TEXT NOT NULL, detail TEXT,
    output TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS agent_steps_task ON agent_steps(task_id, idx);
CREATE TABLE IF NOT EXISTS agent_events(
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL
        REFERENCES agent_tasks(id) ON DELETE CASCADE,
    created_at REAL NOT NULL, type TEXT NOT NULL, payload TEXT);
CREATE INDEX IF NOT EXISTS agent_events_task ON agent_events(task_id, id);
CREATE TABLE IF NOT EXISTS agent_tool_calls(
    id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES agent_tasks(id) ON DELETE CASCADE,
    step_id INTEGER, tool TEXT NOT NULL, args TEXT NOT NULL, status TEXT NOT NULL,
    approval_required INTEGER NOT NULL DEFAULT 0, result TEXT, error TEXT,
    created_at REAL NOT NULL, decided_at REAL);
CREATE INDEX IF NOT EXISTS agent_tool_calls_task ON agent_tool_calls(task_id, created_at);
CREATE TABLE IF NOT EXISTS agent_memory(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, kind TEXT NOT NULL,
    content TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS agent_memory_user ON agent_memory(user_id, updated_at);
CREATE TABLE IF NOT EXISTS agent_artifacts(
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL
        REFERENCES agent_tasks(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL, content TEXT NOT NULL,
    created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS agent_artifacts_task ON agent_artifacts(task_id, updated_at);
"""

PG_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS agent_tasks(
        id TEXT PRIMARY KEY, user_id TEXT NOT NULL, goal TEXT NOT NULL, status TEXT NOT NULL,
        provider TEXT NOT NULL, model TEXT, created_at DOUBLE PRECISION NOT NULL,
        updated_at DOUBLE PRECISION NOT NULL, finished_at DOUBLE PRECISION,
        deadline_at DOUBLE PRECISION NOT NULL, plan TEXT, report TEXT, error TEXT,
        error_code TEXT, pending_call TEXT, ai_calls INTEGER NOT NULL DEFAULT 0,
        tool_calls INTEGER NOT NULL DEFAULT 0, prompt_tokens INTEGER NOT NULL DEFAULT 0,
        completion_tokens INTEGER NOT NULL DEFAULT 0, ip_key TEXT NOT NULL DEFAULT '')""",
    "CREATE INDEX IF NOT EXISTS agent_tasks_user ON agent_tasks(user_id, updated_at)",
    """CREATE TABLE IF NOT EXISTS agent_steps(
        id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES agent_tasks(id) ON DELETE CASCADE,
        idx INTEGER NOT NULL, title TEXT NOT NULL, status TEXT NOT NULL, detail TEXT,
        output TEXT, created_at DOUBLE PRECISION NOT NULL, updated_at DOUBLE PRECISION NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS agent_steps_task ON agent_steps(task_id, idx)",
    """CREATE TABLE IF NOT EXISTS agent_events(
        id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES agent_tasks(id) ON DELETE CASCADE,
        created_at DOUBLE PRECISION NOT NULL, type TEXT NOT NULL, payload TEXT)""",
    "CREATE INDEX IF NOT EXISTS agent_events_task ON agent_events(task_id, id)",
    """CREATE TABLE IF NOT EXISTS agent_tool_calls(
        id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES agent_tasks(id) ON DELETE CASCADE,
        step_id BIGINT, tool TEXT NOT NULL, args TEXT NOT NULL, status TEXT NOT NULL,
        approval_required INTEGER NOT NULL DEFAULT 0, result TEXT, error TEXT,
        created_at DOUBLE PRECISION NOT NULL, decided_at DOUBLE PRECISION)""",
    "CREATE INDEX IF NOT EXISTS agent_tool_calls_task ON agent_tool_calls(task_id, created_at)",
    """CREATE TABLE IF NOT EXISTS agent_memory(
        id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, user_id TEXT NOT NULL,
        kind TEXT NOT NULL, content TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL,
        updated_at DOUBLE PRECISION NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS agent_memory_user ON agent_memory(user_id, updated_at)",
    """CREATE TABLE IF NOT EXISTS agent_artifacts(
        id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES agent_tasks(id) ON DELETE CASCADE,
        user_id TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL, content TEXT NOT NULL,
        created_at DOUBLE PRECISION NOT NULL, updated_at DOUBLE PRECISION NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS agent_artifacts_task ON agent_artifacts(task_id, updated_at)",
]


def _dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _safe_json(raw):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return {"raw": str(raw)[:500]}


def _scalar(row, key):
    if row is None:
        return 0
    if isinstance(row, dict):
        return row.get(key, 0) or 0
    try:
        return int(row[0])
    except (KeyError, IndexError, TypeError, ValueError):
        return 0


class Store:
    """SQL for tasks/steps/events/tool-calls/memory/artifacts on both engines."""

    def __init__(self, db):
        self.db = db

    # -- schema ---------------------------------------------------------------
    def apply_schema(self):
        with self.db.connect() as conn:
            if self.db.postgres:
                for statement in PG_SCHEMA:
                    conn.execute(statement)
            else:
                conn.executescript(SQLITE_SCHEMA)

    def recover_interrupted(self):
        """Mark tasks still 'active' after a process restart as interrupted.

        Without this the UI would show a spinner that can never finish, and the
        per-user active-task guard would lock a visitor out forever.
        """
        now = time.time()
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        with self.db.connect() as conn:
            rows = self.db.run(conn, f"SELECT id FROM agent_tasks WHERE status IN ({placeholders})",
                               ACTIVE_STATUSES).fetchall()
            ids = [row["id"] for row in rows]
            for task_id in ids:
                self.db.run(conn, """UPDATE agent_tasks SET status='interrupted', error=?,
                                     error_code='interrupted', pending_call=NULL, finished_at=?,
                                     updated_at=? WHERE id=?""",
                           ("أوقف تشغيل الخادم هذه المهمة. أنشئها من جديد.", now, now, task_id))
                self._event(conn, task_id, "task.interrupted", {"reason": "process_restart"})
        return len(ids)

    def purge_old(self, retention_seconds):
        cutoff = time.time() - retention_seconds
        with self.db.connect() as conn:
            rows = self.db.run(conn, """SELECT id FROM agent_tasks
                                        WHERE updated_at<? AND finished_at IS NOT NULL""",
                              (cutoff,)).fetchall()
            ids = [row["id"] for row in rows]
            for task_id in ids:
                self.db.run(conn, "DELETE FROM agent_tasks WHERE id=?", (task_id,))
        return len(ids)

    # -- tasks ----------------------------------------------------------------
    def create_task(self, user_id, goal, provider, model, deadline_at, ip_key=""):
        task_id = str(uuid.uuid4())
        now = time.time()
        with self.db.connect() as conn:
            self.db.run(conn, """INSERT INTO agent_tasks(id,user_id,goal,status,provider,model,
                                 created_at,updated_at,deadline_at,ip_key)
                                 VALUES(?,?,?,?,?,?,?,?,?,?)""",
                        (task_id, user_id, goal, "queued", provider, model, now, now,
                         deadline_at, ip_key))
            self._event(conn, task_id, "task.created", {"goal": goal[:400]})
        return task_id

    def get_task(self, task_id, user_id=None):
        with self.db.connect() as conn:
            row = self.db.run(conn, "SELECT * FROM agent_tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                return None
            row = dict(row)
            if user_id and row["user_id"] != user_id:
                return None
            return self._task_view(conn, row)

    def raw_task(self, task_id):
        """Full row for the runtime (the public view never carries user_id/ip_key)."""
        with self.db.connect() as conn:
            row = self.db.run(conn, "SELECT * FROM agent_tasks WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row is not None else None

    def task_status(self, task_id):
        with self.db.connect() as conn:
            row = self.db.run(conn, "SELECT status FROM agent_tasks WHERE id=?", (task_id,)).fetchone()
        return row["status"] if row else None

    def list_tasks(self, user_id, limit=20):
        with self.db.connect() as conn:
            rows = self.db.run(conn, """SELECT * FROM agent_tasks WHERE user_id=?
                                        ORDER BY updated_at DESC LIMIT ?""",
                               (user_id, limit)).fetchall()
            return [self._task_view(conn, dict(row), brief=True) for row in rows]

    def count_active(self, user_id):
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        with self.db.connect() as conn:
            row = self.db.run(conn, f"""SELECT COUNT(1) AS n FROM agent_tasks
                                       WHERE user_id=? AND status IN ({placeholders})""",
                              (user_id, *ACTIVE_STATUSES)).fetchone()
        return _scalar(row, "n")

    def claim(self, task_id):
        """queued -> running, atomically, so a task can never run twice."""
        with self.db.connect() as conn:
            cursor = self.db.run(conn, """UPDATE agent_tasks SET status='running', updated_at=?
                                         WHERE id=? AND status='queued'""", (time.time(), task_id))
            claimed = bool(getattr(cursor, "rowcount", 0))
            if claimed:
                self._event(conn, task_id, "task.running", {})
            return claimed

    def request_cancel(self, task_id, user_id=None):
        """Cooperative cancellation: the loop checks this flag between steps."""
        with self.db.connect() as conn:
            row = self.db.run(conn, "SELECT status FROM agent_tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                return False
            status = dict(row)["status"] if not isinstance(row, dict) else row["status"]
            if status in TERMINAL_STATUSES:
                return False
            self.db.run(conn, "UPDATE agent_tasks SET status='cancelled', updated_at=? WHERE id=?",
                        (time.time(), task_id))
            self._event(conn, task_id, "task.cancel_requested", {"was": status})
            return True

    def set_plan(self, task_id, plan):
        with self.db.connect() as conn:
            self.db.run(conn, "UPDATE agent_tasks SET plan=?, updated_at=? WHERE id=?",
                        (_dumps(plan), time.time(), task_id))
            self._event(conn, task_id, "task.plan",
                        {"steps": [step.get("title", "") for step in plan][:12]})

    def finish(self, task_id, status, report=None, error=None, error_code=None):
        now = time.time()
        with self.db.connect() as conn:
            self.db.run(conn, """UPDATE agent_tasks SET status=?, report=?, error=?, error_code=?,
                                 finished_at=?, updated_at=?, pending_call=NULL WHERE id=?""",
                        (status, report, error, error_code, now, now, task_id))
            self._event(conn, task_id, "task." + status, {"report": (report or "")[:600]})

    def note_usage(self, task_id, ai_calls=0, tool_calls=0, prompt_tokens=0, completion_tokens=0):
        with self.db.connect() as conn:
            self.db.run(conn, """UPDATE agent_tasks SET ai_calls=ai_calls+?, tool_calls=tool_calls+?,
                                 prompt_tokens=prompt_tokens+?, completion_tokens=completion_tokens+?,
                                 updated_at=? WHERE id=?""",
                        (ai_calls, tool_calls, prompt_tokens, completion_tokens, time.time(), task_id))

    def set_pending_call(self, task_id, call_id):
        """Flip between running and awaiting_approval and remember what we wait on."""
        with self.db.connect() as conn:
            self.db.run(conn, """UPDATE agent_tasks SET status=?, pending_call=?, updated_at=?
                                 WHERE id=?""",
                        ("awaiting_approval" if call_id else "running", call_id, time.time(), task_id))
            self._event(conn, task_id, "task.awaiting_approval" if call_id else "task.resumed",
                        {"call_id": call_id} if call_id else {})

    # -- steps ----------------------------------------------------------------
    def add_step(self, task_id, index, title, detail=""):
        now = time.time()
        with self.db.connect() as conn:
            step_id = self.db.insert_returning_id(
                conn, """INSERT INTO agent_steps(task_id,idx,title,status,detail,created_at,updated_at)
                         VALUES(?,?,?,?,?,?,?)""", (task_id, index, title, "pending", detail, now, now))
            self._event(conn, task_id, "step.queued",
                        {"step_id": step_id, "idx": index, "title": title})
        return step_id

    def update_step(self, step_id, status, detail=None, output=None):
        with self.db.connect() as conn:
            fields, params = ["status=?", "updated_at=?"], [status, time.time()]
            if detail is not None:
                fields.append("detail=?")
                params.append(detail[:4000])
            if output is not None:
                fields.append("output=?")
                params.append(output[:8000])
            params.append(step_id)
            self.db.run(conn, "UPDATE agent_steps SET " + ", ".join(fields) + " WHERE id=?", params)
            row = self.db.run(conn, "SELECT task_id, idx, title FROM agent_steps WHERE id=?",
                              (step_id,)).fetchone()
            if row is not None:
                row = dict(row)
                self._event(conn, row["task_id"], "step." + status,
                            {"step_id": step_id, "idx": row["idx"], "title": row["title"]})

    # -- events ---------------------------------------------------------------
    def event(self, task_id, kind, payload=None):
        with self.db.connect() as conn:
            return self._event(conn, task_id, kind, payload or {})

    def _event(self, conn, task_id, kind, payload):
        return self.db.insert_returning_id(
            conn, "INSERT INTO agent_events(task_id,created_at,type,payload) VALUES(?,?,?,?)",
            (task_id, time.time(), kind, _dumps(payload)))

    def events_after(self, task_id, cursor=0, limit=200):
        with self.db.connect() as conn:
            rows = self.db.run(conn, """SELECT id,created_at,type,payload FROM agent_events
                                       WHERE task_id=? AND id>? ORDER BY id LIMIT ?""",
                               (task_id, cursor, limit)).fetchall()
        return [self._event_view(dict(row)) for row in rows]

    def events_since(self, task_id, since=0.0, limit=200):
        with self.db.connect() as conn:
            rows = self.db.run(conn, """SELECT id,created_at,type,payload FROM agent_events
                                       WHERE task_id=? AND created_at>? ORDER BY id LIMIT ?""",
                               (task_id, since, limit)).fetchall()
        return [self._event_view(dict(row)) for row in rows]

    @staticmethod
    def _event_view(item):
        return {"id": item["id"], "at": item["created_at"], "type": item["type"],
                "payload": _safe_json(item.get("payload")) or {}}

    # -- tool calls & approvals ----------------------------------------------
    def create_call(self, task_id, step_id, tool, args, approval_required):
        call_id = "tc_" + uuid.uuid4().hex[:20]
        status = "awaiting_approval" if approval_required else "pending"
        with self.db.connect() as conn:
            self.db.run(conn, """INSERT INTO agent_tool_calls(id,task_id,step_id,tool,args,status,
                                 approval_required,created_at) VALUES(?,?,?,?,?,?,?,?)""",
                        (call_id, task_id, step_id, tool, _dumps(args), status,
                         1 if approval_required else 0, time.time()))
            self._event(conn, task_id, "tool." + status,
                        {"call_id": call_id, "tool": tool, "args": args,
                         "approval_required": bool(approval_required)})
        return call_id

    def get_call(self, call_id, task_id=None):
        with self.db.connect() as conn:
            row = self.db.run(conn, "SELECT * FROM agent_tool_calls WHERE id=?", (call_id,)).fetchone()
            if row is None:
                return None
            row = dict(row)
            if task_id and row["task_id"] != task_id:
                return None
            return row

    def decide_call(self, call_id, approve):
        """Record a human decision. Only a call that is still waiting can move."""
        status = "approved" if approve else "denied"
        with self.db.connect() as conn:
            cursor = self.db.run(conn, """UPDATE agent_tool_calls SET status=?, decided_at=?
                                         WHERE id=? AND status='awaiting_approval'""",
                                 (status, time.time(), call_id))
            moved = bool(getattr(cursor, "rowcount", 0))
            if moved:
                info = self.db.run(conn, "SELECT task_id, tool FROM agent_tool_calls WHERE id=?",
                                   (call_id,)).fetchone()
                if info is not None:
                    info = dict(info)
                    self._event(conn, info["task_id"], "tool." + status,
                                {"call_id": call_id, "tool": info["tool"]})
        return moved

    def complete_call(self, call_id, result=None, error=None, status="done"):
        with self.db.connect() as conn:
            self.db.run(conn, "UPDATE agent_tool_calls SET status=?, result=?, error=? WHERE id=?",
                        (status, _dumps(result) if result is not None else None, error, call_id))
            info = self.db.run(conn, "SELECT task_id, tool FROM agent_tool_calls WHERE id=?",
                               (call_id,)).fetchone()
            if info is not None:
                info = dict(info)
                self._event(conn, info["task_id"], "tool.done" if error is None else "tool.error",
                            {"call_id": call_id, "tool": info["tool"], "error": (error or "")[:300],
                             "result_preview": (json.dumps(result, ensure_ascii=False)[:300]
                                                if result is not None else "")})

    # -- memory ---------------------------------------------------------------
    def remember(self, user_id, kind, content, limit=40):
        now = time.time()
        with self.db.connect() as conn:
            existing = self.db.run(conn, """SELECT id FROM agent_memory WHERE user_id=? AND kind=?
                                           AND content=?""", (user_id, kind, content)).fetchone()
            if existing is not None:
                memory_id = dict(existing)["id"]
                self.db.run(conn, "UPDATE agent_memory SET updated_at=? WHERE id=?", (now, memory_id))
                return memory_id
            memory_id = self.db.insert_returning_id(
                conn, """INSERT INTO agent_memory(user_id,kind,content,created_at,updated_at)
                         VALUES(?,?,?,?,?)""", (user_id, kind, content, now, now))
            total = _scalar(self.db.run(conn, "SELECT COUNT(1) AS n FROM agent_memory WHERE user_id=?",
                                        (user_id,)).fetchone(), "n")
            if total > limit:
                self.db.run(conn, """DELETE FROM agent_memory WHERE user_id=? AND id NOT IN
                                     (SELECT id FROM agent_memory WHERE user_id=?
                                      ORDER BY updated_at DESC LIMIT ?)""",
                            (user_id, user_id, limit))
            return memory_id

    def list_memory(self, user_id, limit=40):
        with self.db.connect() as conn:
            rows = self.db.run(conn, """SELECT id,kind,content,created_at,updated_at FROM agent_memory
                                       WHERE user_id=? ORDER BY updated_at DESC LIMIT ?""",
                               (user_id, limit)).fetchall()
        return [dict(row) for row in rows]

    def memory_count(self, user_id):
        with self.db.connect() as conn:
            row = self.db.run(conn, "SELECT COUNT(1) AS n FROM agent_memory WHERE user_id=?",
                              (user_id,)).fetchone()
        return _scalar(row, "n")

    def delete_memory(self, user_id, memory_id):
        with self.db.connect() as conn:
            cursor = self.db.run(conn, "DELETE FROM agent_memory WHERE user_id=? AND id=?",
                                 (user_id, memory_id))
            return bool(getattr(cursor, "rowcount", 0))

    # -- artifacts (canvas) ---------------------------------------------------
    def put_artifact(self, task_id, user_id, name, kind, content):
        now = time.time()
        with self.db.connect() as conn:
            existing = self.db.run(conn, "SELECT id FROM agent_artifacts WHERE task_id=? AND name=?",
                                   (task_id, name)).fetchone()
            if existing is not None:
                artifact_id = dict(existing)["id"]
                self.db.run(conn, """UPDATE agent_artifacts SET kind=?, content=?, updated_at=?
                                    WHERE id=?""", (kind, content, now, artifact_id))
            else:
                artifact_id = self.db.insert_returning_id(
                    conn, """INSERT INTO agent_artifacts(task_id,user_id,name,kind,content,created_at,updated_at)
                             VALUES(?,?,?,?,?,?,?)""",
                    (task_id, user_id, name, kind, content, now, now))
            self._event(conn, task_id, "artifact.saved",
                        {"artifact_id": artifact_id, "name": name, "kind": kind,
                         "bytes": len(content.encode("utf-8"))})
            return artifact_id

    def list_artifacts(self, task_id):
        with self.db.connect() as conn:
            rows = self.db.run(conn, """SELECT id,name,kind,updated_at,LENGTH(content) AS bytes
                                       FROM agent_artifacts WHERE task_id=? ORDER BY updated_at DESC""",
                               (task_id,)).fetchall()
        return [dict(row) for row in rows]

    def _artifacts(self, conn, task_id):
        rows = self.db.run(conn, """SELECT id,name,kind,updated_at,LENGTH(content) AS bytes
                                   FROM agent_artifacts WHERE task_id=? ORDER BY updated_at DESC""",
                           (task_id,)).fetchall()
        return [dict(row) for row in rows]

    def get_artifact(self, artifact_id, user_id):
        with self.db.connect() as conn:
            row = self.db.run(conn, """SELECT id,task_id,user_id,name,kind,content,updated_at
                                      FROM agent_artifacts WHERE id=?""", (artifact_id,)).fetchone()
            if row is None:
                return None
            row = dict(row)
            return row if row["user_id"] == user_id else None

    def delete_artifact(self, artifact_id, user_id):
        with self.db.connect() as conn:
            cursor = self.db.run(conn, "DELETE FROM agent_artifacts WHERE id=? AND user_id=?",
                                 (artifact_id, user_id))
            return bool(getattr(cursor, "rowcount", 0))

    def delete_task(self, task_id, user_id):
        with self.db.connect() as conn:
            cursor = self.db.run(conn, "DELETE FROM agent_tasks WHERE id=? AND user_id=?",
                                 (task_id, user_id))
            return bool(getattr(cursor, "rowcount", 0))

    # -- shared rate accounting (the chat path uses the same table) ----------
    def attempts_in_window(self, user_id, window=3600):
        with self.db.connect() as conn:
            row = self.db.run(conn, """SELECT COUNT(1) AS n FROM attempts WHERE user_id=?
                                      AND created_at>?""", (user_id, time.time() - window)).fetchone()
        return _scalar(row, "n")

    def note_attempt(self, user_id):
        now = time.time()
        with self.db.connect() as conn:
            self.db.run(conn, "INSERT INTO attempts VALUES(?,?)", (user_id, now))
            self.db.run(conn, "DELETE FROM attempts WHERE created_at<?", (now - 86400,))

    # -- view -----------------------------------------------------------------
    def _task_view(self, conn, row, brief=False):
        view = {
            "id": row["id"], "goal": row["goal"], "status": row["status"],
            "provider": row["provider"], "model": row.get("model"),
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "finished_at": row.get("finished_at"),
            "seconds_left": max(0, round(row["deadline_at"] - time.time(), 1)),
            "plan": _safe_json(row.get("plan")) or [],
            "error": row.get("error"), "error_code": row.get("error_code"),
            "ai_calls": row.get("ai_calls", 0), "tool_calls": row.get("tool_calls", 0),
            "usage": {"prompt_tokens": row.get("prompt_tokens", 0),
                      "completion_tokens": row.get("completion_tokens", 0)},
            "active": row["status"] in ACTIVE_STATUSES,
            "pending_call": row.get("pending_call"),
        }
        if brief:
            return view
        view["report"] = row.get("report")
        view["steps"] = [dict(item) for item in self.db.run(
            conn, """SELECT idx,title,status,detail,output FROM agent_steps
                    WHERE task_id=? ORDER BY idx""", (row["id"],)).fetchall()]
        view["artifacts"] = self._artifacts(conn, row["id"])
        view["calls"] = [{
            "id": item["id"], "tool": item["tool"], "status": item["status"],
            "args": _safe_json(item["args"]), "result": _safe_json(item["result"]),
            "error": item["error"], "approval_required": bool(item["approval_required"]),
        } for item in (dict(raw) for raw in self.db.run(
            conn, """SELECT id,tool,status,args,result,error,approval_required
                    FROM agent_tool_calls WHERE task_id=? ORDER BY created_at""",
            (row["id"],)).fetchall())]
        return view
