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
import threading
import time
import uuid

TERMINAL_STATUSES = ("completed", "failed", "cancelled", "interrupted", "expired")
ACTIVE_STATUSES = ("queued", "running", "awaiting_approval")

DEFAULT_CONNECTORS = [
    {"id": "github", "name": "GitHub", "type": "github"},
    {"id": "notion", "name": "Notion", "type": "notion"},
    {"id": "linear", "name": "Linear", "type": "linear"},
    {"id": "google_drive", "name": "Google Drive", "type": "google_drive"},
    {"id": "google_docs", "name": "Google Docs", "type": "google_docs"},
    {"id": "google_calendar", "name": "Google Calendar", "type": "google_calendar"},
]

SQLITE_TABLES = """
CREATE TABLE IF NOT EXISTS agent_projects(
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL,
    instructions TEXT NOT NULL DEFAULT '', context TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS agent_tasks(
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, goal TEXT NOT NULL, status TEXT NOT NULL,
    provider TEXT NOT NULL, model TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
    finished_at REAL, deadline_at REAL NOT NULL, plan TEXT, report TEXT, error TEXT,
    error_code TEXT, pending_call TEXT, ai_calls INTEGER NOT NULL DEFAULT 0,
    tool_calls INTEGER NOT NULL DEFAULT 0, prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0, ip_key TEXT NOT NULL DEFAULT '',
    project_id TEXT);
CREATE TABLE IF NOT EXISTS agent_steps(
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL
        REFERENCES agent_tasks(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL, title TEXT NOT NULL, status TEXT NOT NULL, detail TEXT,
    output TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS agent_events(
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL
        REFERENCES agent_tasks(id) ON DELETE CASCADE,
    created_at REAL NOT NULL, type TEXT NOT NULL, payload TEXT);
CREATE TABLE IF NOT EXISTS agent_tool_calls(
    id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES agent_tasks(id) ON DELETE CASCADE,
    step_id INTEGER, tool TEXT NOT NULL, args TEXT NOT NULL, status TEXT NOT NULL,
    approval_required INTEGER NOT NULL DEFAULT 0, result TEXT, error TEXT,
    created_at REAL NOT NULL, decided_at REAL);
CREATE TABLE IF NOT EXISTS agent_memory(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, kind TEXT NOT NULL,
    content TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS agent_artifacts(
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL
        REFERENCES agent_tasks(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL, content TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1, created_by TEXT NOT NULL DEFAULT 'agent',
    parent_version INTEGER, project_id TEXT,
    created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS agent_artifact_versions(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artifact_id INTEGER NOT NULL REFERENCES agent_artifacts(id) ON DELETE CASCADE,
    version INTEGER NOT NULL, type TEXT NOT NULL, content TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT 'agent', parent_version INTEGER,
    created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS agent_connectors(
    id TEXT NOT NULL, user_id TEXT NOT NULL, name TEXT NOT NULL,
    type TEXT NOT NULL, connected INTEGER NOT NULL DEFAULT 0,
    files INTEGER NOT NULL DEFAULT 0, messages INTEGER NOT NULL DEFAULT 0,
    external_actions INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL,
    updated_at REAL NOT NULL, PRIMARY KEY (id, user_id));
"""

SQLITE_INDICES = """
CREATE INDEX IF NOT EXISTS agent_projects_user ON agent_projects(user_id, updated_at);
CREATE INDEX IF NOT EXISTS agent_tasks_user ON agent_tasks(user_id, updated_at);
CREATE INDEX IF NOT EXISTS agent_tasks_project ON agent_tasks(project_id);
CREATE INDEX IF NOT EXISTS agent_steps_task ON agent_steps(task_id, idx);
CREATE INDEX IF NOT EXISTS agent_events_task ON agent_events(task_id, id);
CREATE INDEX IF NOT EXISTS agent_tool_calls_task ON agent_tool_calls(task_id, created_at);
CREATE INDEX IF NOT EXISTS agent_memory_user ON agent_memory(user_id, updated_at);
CREATE INDEX IF NOT EXISTS agent_artifacts_task ON agent_artifacts(task_id, updated_at);
CREATE INDEX IF NOT EXISTS agent_artifacts_project ON agent_artifacts(project_id);
CREATE INDEX IF NOT EXISTS agent_art_ver ON agent_artifact_versions(artifact_id, version);
CREATE INDEX IF NOT EXISTS agent_connectors_user ON agent_connectors(user_id);
"""

SQLITE_SCHEMA = SQLITE_TABLES + "\n" + SQLITE_INDICES

PG_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS agent_projects(
        id TEXT PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL,
        instructions TEXT NOT NULL DEFAULT '', context TEXT NOT NULL DEFAULT '',
        created_at DOUBLE PRECISION NOT NULL, updated_at DOUBLE PRECISION NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS agent_projects_user ON agent_projects(user_id, updated_at)",
    """CREATE TABLE IF NOT EXISTS agent_tasks(
        id TEXT PRIMARY KEY, user_id TEXT NOT NULL, goal TEXT NOT NULL, status TEXT NOT NULL,
        provider TEXT NOT NULL, model TEXT, created_at DOUBLE PRECISION NOT NULL,
        updated_at DOUBLE PRECISION NOT NULL, finished_at DOUBLE PRECISION,
        deadline_at DOUBLE PRECISION NOT NULL, plan TEXT, report TEXT, error TEXT,
        error_code TEXT, pending_call TEXT, ai_calls INTEGER NOT NULL DEFAULT 0,
        tool_calls INTEGER NOT NULL DEFAULT 0, prompt_tokens INTEGER NOT NULL DEFAULT 0,
        completion_tokens INTEGER NOT NULL DEFAULT 0, ip_key TEXT NOT NULL DEFAULT '',
        project_id TEXT)""",
    "CREATE INDEX IF NOT EXISTS agent_tasks_user ON agent_tasks(user_id, updated_at)",
    "CREATE INDEX IF NOT EXISTS agent_tasks_project ON agent_tasks(project_id)",
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
        version INTEGER NOT NULL DEFAULT 1, created_by TEXT NOT NULL DEFAULT 'agent',
        parent_version INTEGER, project_id TEXT,
        created_at DOUBLE PRECISION NOT NULL, updated_at DOUBLE PRECISION NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS agent_artifacts_task ON agent_artifacts(task_id, updated_at)",
    "CREATE INDEX IF NOT EXISTS agent_artifacts_project ON agent_artifacts(project_id)",
    """CREATE TABLE IF NOT EXISTS agent_artifact_versions(
        id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        artifact_id BIGINT NOT NULL REFERENCES agent_artifacts(id) ON DELETE CASCADE,
        version INTEGER NOT NULL, type TEXT NOT NULL, content TEXT NOT NULL,
        created_by TEXT NOT NULL DEFAULT 'agent', parent_version INTEGER,
        created_at DOUBLE PRECISION NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS agent_art_ver ON agent_artifact_versions(artifact_id, version)",
    """CREATE TABLE IF NOT EXISTS agent_connectors(
        id TEXT NOT NULL, user_id TEXT NOT NULL, name TEXT NOT NULL,
        type TEXT NOT NULL, connected INTEGER NOT NULL DEFAULT 0,
        files INTEGER NOT NULL DEFAULT 0, messages INTEGER NOT NULL DEFAULT 0,
        external_actions INTEGER NOT NULL DEFAULT 0,
        created_at DOUBLE PRECISION NOT NULL, updated_at DOUBLE PRECISION NOT NULL,
        PRIMARY KEY (id, user_id))""",
    "CREATE INDEX IF NOT EXISTS agent_connectors_user ON agent_connectors(user_id)",
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
    """SQL for tasks/steps/events/tool-calls/memory/artifacts/projects/connectors."""

    def __init__(self, db):
        self.db = db
        self._task_allowances = set()
        self._allowances_lock = threading.Lock()

    # -- task-scoped allowances -----------------------------------------------
    # Task-scoped allowances granted via `allow_task`. Maintained in-memory per
    # (task_id, tool) pair. Transient across container/worker restarts, which safely
    # degrades to re-prompting human approval on subsequent tool calls. Cleared
    # automatically upon reaching any terminal state or when deleting the task.
    def allow_tool_for_task(self, task_id, tool):
        with self._allowances_lock:
            self._task_allowances.add((str(task_id), str(tool)))

    def is_tool_allowed_for_task(self, task_id, tool):
        with self._allowances_lock:
            return (str(task_id), str(tool)) in self._task_allowances

    def clear_task_allowances(self, task_id):
        with self._allowances_lock:
            self._task_allowances = {item for item in self._task_allowances if item[0] != str(task_id)}

    # -- schema ---------------------------------------------------------------
    def apply_schema(self):
        with self.db.connect() as conn:
            if self.db.postgres:
                for statement in PG_SCHEMA:
                    conn.execute(statement)
                # Ensure additive columns on existing databases
                conn.execute("ALTER TABLE agent_tasks ADD COLUMN IF NOT EXISTS project_id TEXT")
                conn.execute("ALTER TABLE agent_artifacts ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1")
                conn.execute("ALTER TABLE agent_artifacts ADD COLUMN IF NOT EXISTS created_by TEXT NOT NULL DEFAULT 'agent'")
                conn.execute("ALTER TABLE agent_artifacts ADD COLUMN IF NOT EXISTS parent_version INTEGER")
                conn.execute("ALTER TABLE agent_artifacts ADD COLUMN IF NOT EXISTS project_id TEXT")
            else:
                conn.executescript(SQLITE_TABLES)
                self._migrate_sqlite_columns(conn)
                conn.executescript(SQLITE_INDICES)

    def _migrate_sqlite_columns(self, conn):
        try:
            task_cols = [row[1] for row in conn.execute("PRAGMA table_info(agent_tasks)").fetchall()]
            if "project_id" not in task_cols:
                conn.execute("ALTER TABLE agent_tasks ADD COLUMN project_id TEXT")
        except Exception:  # noqa: BLE001
            pass
        try:
            art_cols = [row[1] for row in conn.execute("PRAGMA table_info(agent_artifacts)").fetchall()]
            if "project_id" not in art_cols:
                conn.execute("ALTER TABLE agent_artifacts ADD COLUMN project_id TEXT")
            if "version" not in art_cols:
                conn.execute("ALTER TABLE agent_artifacts ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
            if "created_by" not in art_cols:
                conn.execute("ALTER TABLE agent_artifacts ADD COLUMN created_by TEXT NOT NULL DEFAULT 'agent'")
            if "parent_version" not in art_cols:
                conn.execute("ALTER TABLE agent_artifacts ADD COLUMN parent_version INTEGER")
        except Exception:  # noqa: BLE001
            pass

    def recover_interrupted(self):
        """Mark tasks still 'active' after a process restart as interrupted."""
        now = time.time()
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        with self.db.connect() as conn:
            rows = self.db.run(conn, f"SELECT id FROM agent_tasks WHERE status IN ({placeholders})",
                               ACTIVE_STATUSES).fetchall()
            ids = [row["id"] for row in rows]
            for task_id in ids:
                self.clear_task_allowances(task_id)
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
                self.clear_task_allowances(task_id)
                self.db.run(conn, "DELETE FROM agent_tasks WHERE id=?", (task_id,))
        return len(ids)

    # -- projects -------------------------------------------------------------
    def create_project(self, user_id, name, instructions="", context=""):
        project_id = "proj_" + uuid.uuid4().hex[:16]
        now = time.time()
        with self.db.connect() as conn:
            self.db.run(conn, """INSERT INTO agent_projects(id,user_id,name,instructions,context,created_at,updated_at)
                                 VALUES(?,?,?,?,?,?,?)""",
                        (project_id, user_id, name, instructions or "", context or "", now, now))
        return project_id

    def get_project(self, project_id, user_id=None):
        with self.db.connect() as conn:
            row = self.db.run(conn, "SELECT * FROM agent_projects WHERE id=?", (project_id,)).fetchone()
            if row is None:
                return None
            row = dict(row)
            if user_id and row["user_id"] != user_id:
                return None
            return row

    def list_projects(self, user_id):
        with self.db.connect() as conn:
            rows = self.db.run(conn, """SELECT id,user_id,name,instructions,context,created_at,updated_at
                                       FROM agent_projects WHERE user_id=? ORDER BY updated_at DESC""",
                               (user_id,)).fetchall()
            projects = []
            for r in rows:
                item = dict(r)
                p_id = item["id"]
                t_count = _scalar(self.db.run(conn, "SELECT COUNT(1) AS n FROM agent_tasks WHERE project_id=? AND user_id=?", (p_id, user_id)).fetchone(), "n")
                a_count = _scalar(self.db.run(conn, "SELECT COUNT(1) AS n FROM agent_artifacts WHERE project_id=? AND user_id=?", (p_id, user_id)).fetchone(), "n")
                item["task_count"] = t_count
                item["tasks_count"] = t_count
                item["artifact_count"] = a_count
                item["artifacts_count"] = a_count
                projects.append(item)
            return projects

    def get_project_detail(self, project_id, user_id):
        with self.db.connect() as conn:
            row = self.db.run(conn, "SELECT * FROM agent_projects WHERE id=?", (project_id,)).fetchone()
            if row is None:
                return None
            row = dict(row)
            if row["user_id"] != user_id:
                return None
            t_rows = self.db.run(conn, "SELECT * FROM agent_tasks WHERE project_id=? AND user_id=? ORDER BY updated_at DESC LIMIT 50", (project_id, user_id)).fetchall()
            tasks = [self._task_view(conn, dict(tr), brief=True) for tr in t_rows]
            a_rows = self.db.run(conn, """SELECT id,task_id,name,kind,version,created_by,parent_version,project_id,updated_at,LENGTH(content) AS bytes
                                         FROM agent_artifacts WHERE project_id=? AND user_id=? ORDER BY updated_at DESC LIMIT 50""",
                                 (project_id, user_id)).fetchall()
            artifacts = []
            for ar in a_rows:
                a_item = dict(ar)
                a_item["type"] = a_item["kind"]
                a_item["version"] = a_item.get("version") or 1
                artifacts.append(a_item)
            connectors = self.list_connectors(user_id)
            task_ids = [t["id"] for t in tasks]
            activity = []
            if task_ids:
                placeholders = ",".join("?" for _ in task_ids)
                ev_rows = self.db.run(conn, f"""SELECT id,task_id,created_at,type,payload FROM agent_events
                                              WHERE task_id IN ({placeholders}) ORDER BY id DESC LIMIT 30""",
                                      task_ids).fetchall()
                activity = [self._event_view(dict(er)) for er in ev_rows]
            return {
                "project": row,
                "tasks": tasks,
                "artifacts": artifacts,
                "connectors": connectors,
                "activity": activity,
            }

    # -- tasks ----------------------------------------------------------------
    def create_task(self, user_id, goal, provider, model, deadline_at, ip_key="", project_id=None):
        task_id = str(uuid.uuid4())
        now = time.time()
        with self.db.connect() as conn:
            self.db.run(conn, """INSERT INTO agent_tasks(id,user_id,goal,status,provider,model,
                                 created_at,updated_at,deadline_at,ip_key,project_id)
                                 VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        (task_id, user_id, goal, "queued", provider, model, now, now,
                         deadline_at, ip_key, project_id))
            ev = {"goal": goal[:400]}
            if project_id:
                ev["project_id"] = project_id
            self._event(conn, task_id, "task.created", ev)
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

    def list_tasks(self, user_id, limit=20, status=None, project_id=None):
        query = "SELECT * FROM agent_tasks WHERE user_id=?"
        params = [user_id]
        if status and status in (TERMINAL_STATUSES + ACTIVE_STATUSES):
            query += " AND status=?"
            params.append(status)
        if project_id:
            query += " AND project_id=?"
            params.append(project_id)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        with self.db.connect() as conn:
            rows = self.db.run(conn, query, params).fetchall()
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
        self.clear_task_allowances(task_id)
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
            self._event(conn, task_id, "task.cancelled", {"was": status})
            return True

    def set_plan(self, task_id, plan):
        with self.db.connect() as conn:
            self.db.run(conn, "UPDATE agent_tasks SET plan=?, updated_at=? WHERE id=?",
                        (_dumps(plan), time.time(), task_id))
            self._event(conn, task_id, "task.plan",
                        {"steps": [step.get("title", "") for step in plan][:12]})

    def finish(self, task_id, status, report=None, error=None, error_code=None):
        now = time.time()
        self.clear_task_allowances(task_id)
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
                task_id, idx, title = row["task_id"], row["idx"], row["title"]
                self._event(conn, task_id, "step." + status,
                            {"step_id": step_id, "idx": idx, "title": title})
                if status == "running":
                    self._event(conn, task_id, "step.started",
                                {"step_id": step_id, "idx": idx, "title": title})
                elif status == "done":
                    self._event(conn, task_id, "step.completed",
                                {"step_id": step_id, "idx": idx, "title": title, "output": output})
                elif status == "failed":
                    self._event(conn, task_id, "step.failed",
                                {"step_id": step_id, "idx": idx, "title": title, "detail": detail})

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
            rows = self.db.run(conn, """SELECT id,task_id,created_at,type,payload FROM agent_events
                                       WHERE task_id=? AND id>? ORDER BY id LIMIT ?""",
                               (task_id, cursor, limit)).fetchall()
        return [self._event_view(dict(row)) for row in rows]

    def events_since(self, task_id, since=0.0, limit=200):
        with self.db.connect() as conn:
            rows = self.db.run(conn, """SELECT id,task_id,created_at,type,payload FROM agent_events
                                       WHERE task_id=? AND created_at>? ORDER BY id LIMIT ?""",
                               (task_id, since, limit)).fetchall()
        return [self._event_view(dict(row)) for row in rows]

    @staticmethod
    def _event_view(item):
        payload = _safe_json(item.get("payload")) or {}
        created_at = item["created_at"]
        iso_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(created_at))
        step_id = payload.get("step_id")
        return {
            "id": item["id"],
            "task_id": item.get("task_id"),
            "step_id": step_id,
            "at": created_at,
            "ts": iso_ts,
            "type": item["type"],
            "payload": payload,
            "data": payload,
        }

    # -- tool calls & approvals ----------------------------------------------
    def create_call(self, task_id, step_id, tool, args, approval_required):
        call_id = "tc_" + uuid.uuid4().hex[:20]
        status = "awaiting_approval" if approval_required else "pending"
        with self.db.connect() as conn:
            self.db.run(conn, """INSERT INTO agent_tool_calls(id,task_id,step_id,tool,args,status,
                                 approval_required,created_at) VALUES(?,?,?,?,?,?,?,?)""",
                        (call_id, task_id, step_id, tool, _dumps(args), status,
                         1 if approval_required else 0, time.time()))
            event_payload = {
                "call_id": call_id, "tool": tool, "args": args,
                "approval_required": bool(approval_required), "step_id": step_id
            }
            self._event(conn, task_id, "tool.requested", event_payload)
            self._event(conn, task_id, "tool." + status, event_payload)
            if approval_required:
                target = ""
                if isinstance(args, dict):
                    target = str(args.get("url") or args.get("name") or args.get("expression") or "")[:200]
                risk = "external" if tool == "web_fetch" else "high"
                reason = f"الأداة {tool} تتطلب موافقة للتشغيل"
                self._event(conn, task_id, "approval.required", {
                    "call_id": call_id, "approval_id": call_id, "tool": tool,
                    "action": tool, "target": target, "reason": reason, "risk": risk,
                    "step_id": step_id
                })
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

    def decide_call(self, call_id, decision):
        """Record a human decision. Only a call that is still waiting can move."""
        if isinstance(decision, bool):
            decision_str = "allow_once" if decision else "deny"
        elif isinstance(decision, str):
            decision_str = decision if decision in ("allow_once", "allow_task", "deny") else ("allow_once" if decision == "approved" else "deny")
        else:
            decision_str = "deny"

        approve = decision_str in ("allow_once", "allow_task", "approved")
        status = "approved" if approve else "denied"
        with self.db.connect() as conn:
            cursor = self.db.run(conn, """UPDATE agent_tool_calls SET status=?, decided_at=?
                                         WHERE id=? AND status='awaiting_approval'""",
                                 (status, time.time(), call_id))
            moved = bool(getattr(cursor, "rowcount", 0))
            if moved:
                info = self.db.run(conn, "SELECT task_id, tool, step_id FROM agent_tool_calls WHERE id=?",
                                   (call_id,)).fetchone()
                if info is not None:
                    info = dict(info)
                    task_id, tool = info["task_id"], info["tool"]
                    if decision_str == "allow_task":
                        self.allow_tool_for_task(task_id, tool)
                    self._event(conn, task_id, "tool." + status,
                                {"call_id": call_id, "approval_id": call_id, "tool": tool,
                                 "decision": decision_str, "step_id": info.get("step_id")})
        return moved

    def complete_call(self, call_id, result=None, error=None, status="done"):
        with self.db.connect() as conn:
            self.db.run(conn, "UPDATE agent_tool_calls SET status=?, result=?, error=? WHERE id=?",
                        (status, _dumps(result) if result is not None else None, error, call_id))
            info = self.db.run(conn, "SELECT task_id, tool, step_id FROM agent_tool_calls WHERE id=?",
                               (call_id,)).fetchone()
            if info is not None:
                info = dict(info)
                task_id, tool = info["task_id"], info["tool"]
                step_id = info.get("step_id")
                if error is None and result is not None:
                    self._event(conn, task_id, "tool.result",
                                {"call_id": call_id, "tool": tool, "step_id": step_id, "result": result})
                self._event(conn, task_id, "tool.done" if error is None else "tool.error",
                            {"call_id": call_id, "tool": tool, "step_id": step_id,
                             "error": (error or "")[:300],
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
    def put_artifact(self, task_id, user_id, name, kind, content, created_by="agent", project_id=None):
        now = time.time()
        with self.db.connect() as conn:
            if project_id is None and task_id:
                t_row = self.db.run(conn, "SELECT project_id FROM agent_tasks WHERE id=?", (task_id,)).fetchone()
                if t_row:
                    project_id = dict(t_row).get("project_id")
            existing = self.db.run(conn, "SELECT id, version FROM agent_artifacts WHERE task_id=? AND name=?",
                                   (task_id, name)).fetchone()
            if existing is not None:
                existing_dict = dict(existing)
                artifact_id = existing_dict["id"]
                parent_version = existing_dict.get("version") or 1
                new_version = parent_version + 1
                self.db.run(conn, """UPDATE agent_artifacts SET kind=?, content=?, version=?,
                                     parent_version=?, created_by=?, updated_at=?
                                     WHERE id=?""",
                            (kind, content, new_version, parent_version, created_by, now, artifact_id))
            else:
                new_version = 1
                parent_version = None
                artifact_id = self.db.insert_returning_id(
                    conn, """INSERT INTO agent_artifacts(task_id,user_id,name,kind,content,version,created_by,parent_version,project_id,created_at,updated_at)
                             VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (task_id, user_id, name, kind, content, new_version, created_by, parent_version, project_id, now, now))
            self.db.insert_returning_id(
                conn, """INSERT INTO agent_artifact_versions(artifact_id,version,type,content,created_by,parent_version,created_at)
                         VALUES(?,?,?,?,?,?,?)""",
                (artifact_id, new_version, kind, content, created_by, parent_version, now))
            event_payload = {
                "artifact_id": artifact_id, "name": name, "kind": kind, "type": kind,
                "version": new_version, "bytes": len(content.encode("utf-8")),
                "created_by": created_by
            }
            self._event(conn, task_id, "artifact.saved", event_payload)
            self._event(conn, task_id, "artifact.created", event_payload)
            return artifact_id

    def list_artifacts(self, task_id):
        with self.db.connect() as conn:
            return self._artifacts(conn, task_id)

    def _artifacts(self, conn, task_id):
        rows = self.db.run(conn, """SELECT id,name,kind,version,created_by,parent_version,project_id,updated_at,LENGTH(content) AS bytes
                                   FROM agent_artifacts WHERE task_id=? ORDER BY updated_at DESC""",
                           (task_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["type"] = item["kind"]
            item["version"] = item.get("version") or 1
            item["created_by"] = item.get("created_by") or "agent"
            result.append(item)
        return result

    def get_artifact(self, artifact_id, user_id):
        with self.db.connect() as conn:
            row = self.db.run(conn, """SELECT id,task_id,user_id,name,kind,content,version,created_by,parent_version,project_id,created_at,updated_at
                                      FROM agent_artifacts WHERE id=?""", (artifact_id,)).fetchone()
            if row is None:
                return None
            row = dict(row)
            if row["user_id"] != user_id:
                return None
            row["type"] = row["kind"]
            row["version"] = row.get("version") or 1
            row["created_by"] = row.get("created_by") or "agent"
            return row

    def get_artifact_versions(self, artifact_id, user_id):
        with self.db.connect() as conn:
            art = self.db.run(conn, "SELECT id, user_id FROM agent_artifacts WHERE id=?", (artifact_id,)).fetchone()
            if art is None or dict(art)["user_id"] != user_id:
                return None
            rows = self.db.run(conn, """SELECT id,artifact_id,version,type,content,created_by,parent_version,created_at
                                       FROM agent_artifact_versions WHERE artifact_id=?
                                       ORDER BY version DESC""", (artifact_id,)).fetchall()
            versions = []
            for r in rows:
                item = dict(r)
                item["bytes"] = len(item["content"].encode("utf-8"))
                versions.append(item)
            return versions

    def delete_artifact(self, artifact_id, user_id):
        with self.db.connect() as conn:
            cursor = self.db.run(conn, "DELETE FROM agent_artifacts WHERE id=? AND user_id=?",
                                 (artifact_id, user_id))
            return bool(getattr(cursor, "rowcount", 0))

    def delete_task(self, task_id, user_id):
        self.clear_task_allowances(task_id)
        with self.db.connect() as conn:
            cursor = self.db.run(conn, "DELETE FROM agent_tasks WHERE id=? AND user_id=?",
                                 (task_id, user_id))
            return bool(getattr(cursor, "rowcount", 0))

    # -- connectors -----------------------------------------------------------
    def list_connectors(self, user_id):
        with self.db.connect() as conn:
            rows = self.db.run(conn, "SELECT * FROM agent_connectors WHERE user_id=?", (user_id,)).fetchall()
            stored = {row["id"]: dict(row) for row in rows}
            connectors = []
            for spec in DEFAULT_CONNECTORS:
                cid = spec["id"]
                if cid in stored:
                    r = stored[cid]
                    connectors.append({
                        "id": cid,
                        "name": spec["name"],
                        "type": spec["type"],
                        "connected": bool(r.get("connected", 0)),
                        "permissions": {
                            "files": bool(r.get("files", 0)),
                            "messages": bool(r.get("messages", 0)),
                            "external_actions": bool(r.get("external_actions", 0)),
                        },
                        "updated_at": r.get("updated_at"),
                    })
                else:
                    connectors.append({
                        "id": cid,
                        "name": spec["name"],
                        "type": spec["type"],
                        "connected": False,
                        "permissions": {
                            "files": False,
                            "messages": False,
                            "external_actions": False,
                        },
                        "updated_at": None,
                    })
            return connectors

    def update_connector_permissions(self, user_id, connector_id, permissions):
        spec = next((c for c in DEFAULT_CONNECTORS if c["id"] == connector_id), None)
        if not spec:
            return None
        now = time.time()
        files = 1 if permissions.get("files") else 0
        messages = 1 if permissions.get("messages") else 0
        external_actions = 1 if permissions.get("external_actions") else 0
        with self.db.connect() as conn:
            existing = self.db.run(conn, "SELECT * FROM agent_connectors WHERE id=? AND user_id=?",
                                   (connector_id, user_id)).fetchone()
            if existing is not None:
                self.db.run(conn, """UPDATE agent_connectors SET files=?, messages=?, external_actions=?, updated_at=?
                                     WHERE id=? AND user_id=?""",
                            (files, messages, external_actions, now, connector_id, user_id))
            else:
                self.db.run(conn, """INSERT INTO agent_connectors(id,user_id,name,type,connected,files,messages,external_actions,created_at,updated_at)
                                     VALUES(?,?,?,?,?,?,?,?,?,?)""",
                            (connector_id, user_id, spec["name"], spec["type"], 0, files, messages, external_actions, now, now))
        return {
            "id": connector_id,
            "name": spec["name"],
            "type": spec["type"],
            "connected": False,
            "permissions": {
                "files": bool(files),
                "messages": bool(messages),
                "external_actions": bool(external_actions),
            },
            "updated_at": now,
        }

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
            "project_id": row.get("project_id"),
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
