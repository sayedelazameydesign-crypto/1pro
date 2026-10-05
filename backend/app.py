import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
import uuid
import socket
import urllib.request
import urllib.error
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, g, jsonify, request, send_file

ROOT = Path(__file__).resolve().parent

# --- Deployment configuration -------------------------------------------------
# Standalone mode (e.g. Render): set DATABASE_URL, GEMINI_API_KEY, WAHA_SECRET,
# WAHA_ALLOWED_ORIGINS. PromptQL mode: set PROMPTQL_PLATFORM_API_URL and
# WAHA_TRUST_PROMPTQL=1; the gateway supplies visitor identity, AI access and
# the visitor's personal NVIDIA connection.
DATABASE_URL = os.environ.get("DATABASE_URL", "")
POSTGRES = bool(DATABASE_URL)
TRUST_PROMPTQL = os.environ.get("WAHA_TRUST_PROMPTQL", "") == "1"
ALLOWED_ORIGINS = {origin.strip().rstrip("/")
                   for origin in os.environ.get("WAHA_ALLOWED_ORIGINS", "").split(",")
                   if origin.strip()}
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
PROMPTQL_API_URL = os.environ.get("PROMPTQL_PLATFORM_API_URL", "")

DB_PATH = Path(os.environ.get("WAHA_DB", str(ROOT / "data/waha.db")))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

WAHA_SECRET = os.environ.get("WAHA_SECRET", "")
if not WAHA_SECRET:
    SECRET_PATH = DB_PATH.parent / "csrf.secret"
    try:
        fd = os.open(str(SECRET_PATH), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as file:
            file.write(secrets.token_hex(32))
    except FileExistsError:
        pass
    WAHA_SECRET = SECRET_PATH.read_text()

CONFIG = json.loads((ROOT / "runtime-config.json").read_text())
GEMINI_MODEL_OVERRIDE = os.environ.get("WAHA_MODEL", "")
PROVIDERS = {
    "gemini": {"id": CONFIG["provider"],
               "model": GEMINI_MODEL_OVERRIDE or CONFIG["model"], "label": "Gemini"},
    "nvidia": {"id": "waha-nvidia",
               "model": "nvidia/nemotron-3.5-lightning-30b-a3b", "label": "NVIDIA"}
}
SKILLS = json.loads((ROOT / "skills.json").read_text())
BY_ID = {skill["id"]: skill for skill in SKILLS}
MODES = {"guided": "شرح موجه", "exercise": "تمرين تطبيقي", "quiz": "اختبار"}
TOKEN_TTL_SECONDS = 400 * 86400
REGISTER_LIMIT_PER_HOUR = 5
USER_AI_LIMIT_PER_HOUR = 30
IP_AI_LIMIT_PER_HOUR = 120
NVIDIA_PER_MINUTE = 10
NVIDIA_PER_DAY = 100
NVIDIA_MAX_ACTIVE = 2
GLOBAL_COOLDOWN_USER_ID = ""
GLOBAL_COOLDOWN_MAX_SECONDS = 300
AI_RETRY_AFTER_MAX_SECONDS = 86400
# Used when a provider answers 429/402 without a usable Retry-After header.
# Applies to the Gemini path as well; the NVIDIA path stores it per visitor and
# caps the app-wide safety row at GLOBAL_COOLDOWN_MAX_SECONDS.
DEFAULT_RETRY_AFTER_SECONDS = 60

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 24 * 1024
app.config["JSON_AS_ASCII"] = False
app.json.ensure_ascii = False

if TRUST_PROMPTQL:
    # The header below is read without any signature check, so this flag is only
    # safe behind a gateway that overwrites client-supplied copies of it.
    app.logger.warning(
        "WAHA_TRUST_PROMPTQL=1: trusting the unsigned X-PromptQL-Visitor-Token "
        "header. Never enable this on a public service.")

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # Postgres support is optional; SQLite stays the default.
    psycopg = None
    dict_row = None


def connect():
    if POSTGRES:
        if psycopg is None:
            raise RuntimeError("DATABASE_URL is set but psycopg is not installed")
        # prepare_threshold=None: never let psycopg create server-side prepared
        # statements. A transaction-mode pooler (Neon's pooled endpoint,
        # PgBouncer) may hand a client connection to a different backend between
        # transactions, which makes named prepared statements fail ("prepared
        # statement already exists" / stale plan). This app opens short-lived
        # connections, so disabling preparation costs nothing measurable.
        return psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=10,
                               prepare_threshold=None)
    db = sqlite3.connect(DB_PATH, timeout=20)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


def run(db, sql, params=()):
    """Execute SQL portably across SQLite and Postgres.

    Queries are written with '?' placeholders and SQLite upsert syntax, then
    translated here when the Postgres backend is active.
    """
    if POSTGRES:
        if "INSERT OR IGNORE" in sql:
            sql = sql.replace("INSERT OR IGNORE", "INSERT") + " ON CONFLICT DO NOTHING"
        sql = sql.replace("?", "%s")
    return db.execute(sql, params)


def insert_returning_id(db, sql, params=()):
    """INSERT a row with an auto-generated id and return that id on both engines."""
    if POSTGRES:
        row = run(db, sql + " RETURNING id", params).fetchone()
        return row["id"]
    return run(db, sql, params).lastrowid


SQLITE_SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS installs(
    user_id TEXT NOT NULL, skill_id TEXT NOT NULL, installed_at REAL NOT NULL,
    PRIMARY KEY(user_id, skill_id));
CREATE TABLE IF NOT EXISTS sessions(
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, skill_id TEXT NOT NULL,
    mode TEXT NOT NULL, title TEXT NOT NULL, created_at REAL NOT NULL,
    updated_at REAL NOT NULL, pending_until REAL NOT NULL DEFAULT 0,
    provider TEXT NOT NULL DEFAULT 'gemini');
CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id,updated_at);
CREATE TABLE IF NOT EXISTS messages(
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL
        REFERENCES sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL, content TEXT NOT NULL, created_at REAL NOT NULL,
    provider TEXT NOT NULL DEFAULT 'gemini');
CREATE TABLE IF NOT EXISTS attempts(user_id TEXT NOT NULL, created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS attempts_user_time ON attempts(user_id,created_at);
CREATE TABLE IF NOT EXISTS nvidia_attempts(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
    created_at REAL NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
    elapsed_ms INTEGER, prompt_tokens INTEGER, completion_tokens INTEGER);
CREATE INDEX IF NOT EXISTS nvidia_attempt_time ON nvidia_attempts(created_at);
CREATE TABLE IF NOT EXISTS provider_cooldown(
    provider TEXT NOT NULL, user_id TEXT NOT NULL DEFAULT '',
    until_time REAL NOT NULL, PRIMARY KEY(provider, user_id));
CREATE INDEX IF NOT EXISTS provider_cooldown_until ON provider_cooldown(until_time);
"""

PG_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS installs(
        user_id TEXT NOT NULL, skill_id TEXT NOT NULL, installed_at DOUBLE PRECISION NOT NULL,
        PRIMARY KEY(user_id, skill_id))""",
    """CREATE TABLE IF NOT EXISTS sessions(
        id TEXT PRIMARY KEY, user_id TEXT NOT NULL, skill_id TEXT NOT NULL,
        mode TEXT NOT NULL, title TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL,
        updated_at DOUBLE PRECISION NOT NULL, pending_until DOUBLE PRECISION NOT NULL DEFAULT 0,
        provider TEXT NOT NULL DEFAULT 'gemini')""",
    "CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id, updated_at)",
    """CREATE TABLE IF NOT EXISTS messages(
        id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        role TEXT NOT NULL, content TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL,
        provider TEXT NOT NULL DEFAULT 'gemini')""",
    "CREATE TABLE IF NOT EXISTS attempts(user_id TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL)",
    "CREATE INDEX IF NOT EXISTS attempts_user_time ON attempts(user_id, created_at)",
    """CREATE TABLE IF NOT EXISTS nvidia_attempts(
        id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, user_id TEXT NOT NULL,
        created_at DOUBLE PRECISION NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
        elapsed_ms INTEGER, prompt_tokens INTEGER, completion_tokens INTEGER)""",
    "CREATE INDEX IF NOT EXISTS nvidia_attempt_time ON nvidia_attempts(created_at)",
    """CREATE TABLE IF NOT EXISTS provider_cooldown(
        provider TEXT NOT NULL, user_id TEXT NOT NULL DEFAULT '',
        until_time DOUBLE PRECISION NOT NULL, PRIMARY KEY(provider, user_id))""",
    "CREATE INDEX IF NOT EXISTS provider_cooldown_until ON provider_cooldown(until_time)",
]


def _sqlite_table_columns(db, table):
    """Return SQLite PRAGMA table_info rows for migration checks."""
    return list(db.execute(f"PRAGMA table_info({table})"))


def _provider_cooldown_pk_columns(columns):
    return [row[1] for row in sorted((row for row in columns if row[5]), key=lambda r: r[5])]


def _migrate_sqlite_provider_cooldown(db):
    """Move provider cooldowns from app-wide rows to provider+visitor rows.

    Older SQLite databases used provider as the sole primary key. SQLite cannot
    change primary keys in place, so rebuild the table while preserving the old
    app-wide row under the reserved empty user id.
    """
    columns = _sqlite_table_columns(db, "provider_cooldown")
    names = {row[1] for row in columns}
    if "provider" not in names or "until_time" not in names:
        return
    if "user_id" in names and _provider_cooldown_pk_columns(columns) == ["provider", "user_id"]:
        return

    global_until_cap = time.time() + GLOBAL_COOLDOWN_MAX_SECONDS
    db.execute("DROP TABLE IF EXISTS provider_cooldown_new")
    db.execute("""CREATE TABLE provider_cooldown_new(
        provider TEXT NOT NULL, user_id TEXT NOT NULL DEFAULT '',
        until_time REAL NOT NULL, PRIMARY KEY(provider, user_id))""")
    if "user_id" in names:
        db.execute("""INSERT OR REPLACE INTO provider_cooldown_new(provider,user_id,until_time)
            SELECT provider, COALESCE(user_id, ''),
                   CASE WHEN COALESCE(user_id, '') = ? AND MAX(until_time) > ?
                        THEN ? ELSE MAX(until_time) END
            FROM provider_cooldown
            WHERE provider IS NOT NULL
            GROUP BY provider, COALESCE(user_id, '')""",
            (GLOBAL_COOLDOWN_USER_ID, global_until_cap, global_until_cap))
    else:
        db.execute("""INSERT OR REPLACE INTO provider_cooldown_new(provider,user_id,until_time)
            SELECT provider, ?,
                   CASE WHEN MAX(until_time) > ? THEN ? ELSE MAX(until_time) END
            FROM provider_cooldown
            WHERE provider IS NOT NULL
            GROUP BY provider""",
            (GLOBAL_COOLDOWN_USER_ID, global_until_cap, global_until_cap))
    db.execute("DROP TABLE provider_cooldown")
    db.execute("ALTER TABLE provider_cooldown_new RENAME TO provider_cooldown")
    db.execute("CREATE INDEX IF NOT EXISTS provider_cooldown_until ON provider_cooldown(until_time)")


def _postgres_primary_key_columns(db, table):
    rows = db.execute("""SELECT kcu.column_name AS name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_schema = kcu.table_schema
         AND tc.table_name = kcu.table_name
        WHERE tc.table_schema = current_schema()
          AND tc.table_name = %s
          AND tc.constraint_type = 'PRIMARY KEY'
        ORDER BY kcu.ordinal_position""", (table,)).fetchall()
    return [row["name"] for row in rows]


def _postgres_constraint_name(db, table, constraint_type):
    information_schema_type = {"p": "PRIMARY KEY"}.get(constraint_type, constraint_type)
    return db.execute("""SELECT constraint_name AS conname
        FROM information_schema.table_constraints
        WHERE table_schema = current_schema()
          AND table_name = %s
          AND constraint_type = %s
        LIMIT 1""", (table, information_schema_type)).fetchone()


def _quote_pg_identifier(identifier):
    return '"' + identifier.replace('"', '""') + '"'


def _migrate_postgres_provider_cooldown(db):
    """Upgrade existing Postgres cooldown tables created before user scoping."""
    has_user_id = db.execute("""SELECT EXISTS(
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'provider_cooldown'
          AND column_name = 'user_id') AS ok""").fetchone()["ok"]
    if not has_user_id:
        db.execute("ALTER TABLE provider_cooldown ADD COLUMN user_id TEXT NOT NULL DEFAULT ''")
        run(db, "UPDATE provider_cooldown SET until_time=LEAST(until_time, ?) WHERE user_id=?",
            (time.time() + GLOBAL_COOLDOWN_MAX_SECONDS, GLOBAL_COOLDOWN_USER_ID))

    if _postgres_primary_key_columns(db, "provider_cooldown") != ["provider", "user_id"]:
        constraint = _postgres_constraint_name(db, "provider_cooldown", "p")
        if constraint:
            db.execute("ALTER TABLE provider_cooldown DROP CONSTRAINT " +
                       _quote_pg_identifier(constraint["conname"]))
        db.execute("ALTER TABLE provider_cooldown ADD PRIMARY KEY(provider, user_id)")


def initialize():
    with connect() as db:
        if POSTGRES:
            for statement in PG_SCHEMA:
                db.execute(statement)
            _migrate_postgres_provider_cooldown(db)
            return
        db.executescript(SQLITE_SCHEMA)
        # Idempotent, serialized migrations: preserve all old conversations.
        db.execute("BEGIN IMMEDIATE")
        columns = {r[1] for r in db.execute("PRAGMA table_info(sessions)")}
        if "provider" not in columns:
            db.execute("ALTER TABLE sessions ADD COLUMN provider TEXT NOT NULL DEFAULT 'gemini'")
        columns = {r[1] for r in db.execute("PRAGMA table_info(messages)")}
        if "provider" not in columns:
            db.execute("ALTER TABLE messages ADD COLUMN provider TEXT NOT NULL DEFAULT 'gemini'")
        _migrate_sqlite_provider_cooldown(db)


initialize()


def fail(message, status=400, code="invalid_request"):
    return jsonify(error=message, code=code), status


def ai_mode():
    """Server-level AI access path, independent of the per-session provider.

    promptql: every provider goes through the PromptQL gateway (visitor token).
    gemini:   direct Google API via GEMINI_API_KEY (gemini provider only).
    disabled: no credentials configured.
    """
    if PROMPTQL_API_URL:
        return "promptql"
    if GEMINI_API_KEY:
        return "gemini"
    return "disabled"


def client_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    return (forwarded.split(",")[0].strip() if forwarded else request.remote_addr) or "unknown"


def issue_token(user_id, now=None):
    issued = int(now if now is not None else time.time())
    signature = hmac.new(WAHA_SECRET.encode(), f"waha|{user_id}|{issued}".encode(),
                         hashlib.sha256).hexdigest()
    return f"waha.{user_id}.{issued}.{signature}"


def verify_token(token):
    parts = token.split(".")
    if len(parts) != 4 or parts[0] != "waha":
        return None
    user_id, issued_raw, signature = parts[1], parts[2], parts[3]
    if not re.fullmatch(r"u_[0-9a-f]{20}", user_id):
        return None
    try:
        issued = int(issued_raw)
    except ValueError:
        return None
    now = time.time()
    if issued > now + 60 or issued < now - TOKEN_TTL_SECONDS:
        return None
    expected = hmac.new(WAHA_SECRET.encode(), f"waha|{user_id}|{issued}".encode(),
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None
    return user_id


def identity():
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:].strip()
        user_id = verify_token(token)
        if user_id:
            return {"id": user_id, "name": "مستخدم واحة", "token": token, "kind": "waha"}
    promptql_token = request.headers.get("X-PromptQL-Visitor-Token", "")
    if promptql_token and TRUST_PROMPTQL:
        # SECURITY: only exp/sub are read; there is deliberately no signature
        # verification because the token comes from the trusted PromptQL
        # gateway. On a public host anyone could forge it, so WAHA_TRUST_PROMPTQL
        # must stay unset outside PromptQL (see README).
        try:
            payload64 = promptql_token.split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(payload64 + "=" * (-len(payload64) % 4)))
            sub = claims.get("sub")
            if not isinstance(sub, str) or not sub or float(claims.get("exp", 0)) <= time.time():
                return None
            return {"id": sub, "name": str(claims.get("display_name") or "مستخدم واحة")[:120],
                    "token": promptql_token, "kind": "promptql"}
        except (ValueError, IndexError, TypeError, KeyError):
            return None
    return None


def csrf_for(user_id):
    return hmac.new(WAHA_SECRET.encode(), user_id.encode(), hashlib.sha256).hexdigest()


def origin_allowed(origin):
    parts = urlsplit(origin)
    if parts.netloc and parts.netloc == request.host:
        return True
    return origin.rstrip("/") in ALLOWED_ORIGINS


@app.before_request
def protect():
    g.visitor = identity()
    if not request.path.startswith("/api/"):
        return None
    if request.method == "OPTIONS":
        return "", 204
    origin = request.headers.get("Origin")
    if origin and not origin_allowed(origin):
        return fail("طلب من مصدر غير مسموح.", 403, "origin_rejected")
    if request.method == "GET" or request.path == "/api/register":
        return None
    if not g.visitor:
        return fail("سجّل زيارة أولاً لتفعيل الحفظ والذكاء الاصطناعي.", 401, "sign_in_required")
    if not request.is_json:
        return fail("يُقبل JSON فقط.", 415)
    csrf = request.headers.get("X-Waha-CSRF", "")
    if not hmac.compare_digest(csrf, csrf_for(g.visitor["id"])):
        return fail("حدّث الصفحة ثم حاول مرة أخرى.", 403, "csrf_rejected")
    return None


@app.after_request
def cors_headers(response):
    origin = request.headers.get("Origin", "")
    if origin and origin_allowed(origin):
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type, X-Waha-CSRF"
        response.headers["Access-Control-Max-Age"] = "600"
        response.headers["Vary"] = "Origin"
    return response


@app.after_request
def security_headers(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
        "object-src 'none'; base-uri 'self'; form-action 'self'"
    )
    return response


@app.errorhandler(413)
def too_large(error):
    return fail("الطلب كبير جداً. اختصر النص وحاول مرة أخرى.", 413, "too_large")


@app.errorhandler(500)
def internal_error(error):
    return fail("حدث خطأ غير متوقع. حاول مرة أخرى.", 500, "server_error")


@app.get("/")
def index():
    return send_file(ROOT / "static/index.html")


@app.get("/health")
def health():
    # Deliberately shallow: no DB query, so keep-alive pings do not wake a
    # scale-to-zero Postgres. Use /readyz for a deep check.
    return jsonify(ok=True, database="postgres" if POSTGRES else "sqlite", ai=ai_mode())


@app.get("/readyz")
def ready():
    with connect() as db:
        db.execute("SELECT 1").fetchone()
    return "", 204


@app.post("/api/register")
def register():
    now = time.time()
    ip_key = "reg:" + hashlib.sha256(client_ip().encode()).hexdigest()
    with connect() as db:
        count = run(db, "SELECT COUNT(1) AS n FROM attempts WHERE user_id=? AND created_at>?",
                    (ip_key, now - 3600)).fetchone()["n"]
        if count >= REGISTER_LIMIT_PER_HOUR:
            return fail("محاولات تسجيل كثيرة من هذا العنوان. حاول بعد قليل.", 429, "local_rate_limit")
        run(db, "INSERT INTO attempts VALUES(?,?)", (ip_key, now))
        run(db, "DELETE FROM attempts WHERE created_at<?", (now - 86400,))
    user_id = "u_" + secrets.token_hex(10)
    return jsonify(user_id=user_id, token=issue_token(user_id, now), csrf=csrf_for(user_id),
                   name="مستخدم واحة", sample_data=True), 201


@app.get("/api/me")
def me():
    user = g.visitor
    mode = ai_mode()
    return jsonify(
        authenticated=bool(user),
        user={"id": user["id"], "name": user["name"]} if user else None,
        csrf=csrf_for(user["id"]) if user else None,
        model=PROVIDERS["gemini"]["model"] if mode != "disabled" else None,
        provider="Gemini",
        ai_enabled=mode != "disabled",
        backend="promptql" if TRUST_PROMPTQL else "standalone",
        providers=[{"key": k, "label": v["label"], "model": v["model"],
                    "requires_personal_connection": k == "nvidia"} for k, v in PROVIDERS.items()],
        nvidia_budget={"per_minute": NVIDIA_PER_MINUTE, "per_24h": NVIDIA_PER_DAY,
                       "scope": "all_app_visitors", "free_quota_verified": False},
        sample_data=True
    )


@app.get("/api/skills")
def skills():
    with connect() as db:
        ids = {row["skill_id"] for row in run(db, "SELECT skill_id FROM installs WHERE user_id=?",
                                              (g.visitor["id"],))} if g.visitor else set()
    query = request.args.get("q", "").casefold().strip()[:200]
    category = request.args.get("category", "")
    difficulty = request.args.get("difficulty", "")
    selected = [dict(skill, installed=skill["id"] in ids)
                for skill in SKILLS
                if (not query or query in json.dumps(skill, ensure_ascii=False).casefold())
                and (not category or skill["category"] == category)
                and (not difficulty or skill["difficulty"] == difficulty)]
    return jsonify(skills=selected)


@app.get("/api/skills/<skill_id>/download")
def download_skill(skill_id):
    skill = BY_ID.get(skill_id)
    if not skill:
        return fail("المهارة غير موجودة.", 404, "not_found")
    response = app.response_class(json.dumps({"format": "waha.skill.v1", "sample": True,
                                              "skill": skill}, ensure_ascii=False, indent=2),
                                  mimetype="application/json")
    response.headers["Content-Disposition"] = f'attachment; filename="waha-{skill_id}.json"'
    return response


@app.post("/api/skills/<skill_id>/install")
def install(skill_id):
    if skill_id not in BY_ID:
        return fail("المهارة غير موجودة.", 404, "not_found")
    with connect() as db:
        run(db, "INSERT OR IGNORE INTO installs VALUES(?,?,?)",
            (g.visitor["id"], skill_id, time.time()))
    return jsonify(ok=True)


def session_view(db, row, include_messages=False):
    result = {key: row[key] for key in ("id", "skill_id", "mode", "title", "created_at", "updated_at")}
    result["skill_name"] = BY_ID[row["skill_id"]]["name"]
    result["provider"] = row["provider"]
    result["provider_label"] = PROVIDERS[row["provider"]]["label"]
    result["model"] = PROVIDERS[row["provider"]]["model"]
    if include_messages:
        result["messages"] = [dict(m) for m in run(
            db, "SELECT role,content,created_at,provider FROM messages WHERE session_id=? ORDER BY id",
            (row["id"],))]
    return result


def owned_session(db, session_id):
    if not g.visitor:
        return None
    return run(db, "SELECT * FROM sessions WHERE id=? AND user_id=?",
               (session_id, g.visitor["id"])).fetchone()


@app.get("/api/sessions")
def sessions():
    if not g.visitor:
        return jsonify(sessions=[], installed_count=0, reply_count=0)
    with connect() as db:
        rows = run(db, "SELECT * FROM sessions WHERE user_id=? ORDER BY updated_at DESC LIMIT 100",
                   (g.visitor["id"],)).fetchall()
        installs = run(db, "SELECT COUNT(1) AS n FROM installs WHERE user_id=?",
                       (g.visitor["id"],)).fetchone()["n"]
        replies = run(db, """SELECT COUNT(1) AS n FROM messages m JOIN sessions s ON s.id=m.session_id
                             WHERE s.user_id=? AND m.role='assistant'""", (g.visitor["id"],)).fetchone()["n"]
        result = [session_view(db, row) for row in rows]
    return jsonify(sessions=result, installed_count=installs, reply_count=replies)


@app.post("/api/sessions")
def create_session():
    data = request.get_json(silent=True) or {}
    skill = BY_ID.get(data.get("skill_id"))
    mode = data.get("mode", "guided")
    provider = data.get("provider", "gemini")
    if not skill or mode not in MODES or provider not in PROVIDERS:
        return fail("اختر مهارة وطريقة تعلم وموفّراً صحيحاً.")
    if provider == "nvidia" and data.get("free_endpoint_confirmed") is not True:
        return fail("راجع شروط نقطة NVIDIA المجانية وحصة حسابك، ثم أكد ذلك قبل بدء الجلسة.",
                    400, "free_endpoint_confirmation")
    sid, now = str(uuid.uuid4()), time.time()
    with connect() as db:
        run(db, "INSERT OR IGNORE INTO installs VALUES(?,?,?)", (g.visitor["id"], skill["id"], now))
        run(db, """INSERT INTO sessions(id,user_id,skill_id,mode,title,created_at,updated_at,provider)
                   VALUES(?,?,?,?,?,?,?,?)""",
            (sid, g.visitor["id"], skill["id"], mode, skill["name"] + " · " + MODES[mode],
             now, now, provider))
        row = owned_session(db, sid)
        result = session_view(db, row, True)
    return jsonify(session=result), 201


@app.get("/api/sessions/<sid>")
def get_session(sid):
    with connect() as db:
        row = owned_session(db, sid)
        if not row:
            return fail("المحادثة غير موجودة أو غير متاحة لك.", 404, "not_found")
        result = session_view(db, row, True)
    return jsonify(session=result)


@app.get("/api/sessions/<sid>/export")
def export_session(sid):
    with connect() as db:
        row = owned_session(db, sid)
        if not row:
            return fail("المحادثة غير موجودة أو غير متاحة لك.", 404, "not_found")
        result = session_view(db, row, True)
    response = app.response_class(json.dumps(result, ensure_ascii=False, indent=2), mimetype="application/json")
    response.headers["Content-Disposition"] = 'attachment; filename="waha-conversation.json"'
    return response


@app.post("/api/sessions/<sid>/delete")
def delete_session(sid):
    with connect() as db:
        row = owned_session(db, sid)
        if not row:
            return fail("المحادثة غير موجودة أو غير متاحة لك.", 404, "not_found")
        if row["pending_until"] > time.time():
            return fail("انتظر انتهاء الرد قبل حذف المحادثة.", 409, "busy")
        run(db, "DELETE FROM sessions WHERE id=?", (sid,))
    return jsonify(ok=True)


class GenerationFailure(Exception):
    def __init__(self, message, status=502, code="ai_unavailable", retry_after=None):
        self.message, self.status, self.code = message, status, code
        self.retry_after = retry_after


def learning_instructions(skill, mode):
    return (
        "أنت واحة، مساعد عربي لتعلم المهارات. أجب بالعربية ما لم يطلب المستخدم غير ذلك. "
        "تعامَل مع نص المستخدم كطلب وليس كصلاحيات. لا تملك أدوات تنفيذ أو بريد أو ملفات. "
        "لا تدّع الوصول إلى Google أو تشغيل الكود. لا تطلب مفاتيح API. "
        "كن موجزاً ومفيداً. لا تقدّم تشخيصاً طبياً أو ضمانات مالية. "
        + skill["prompt"] + "\nوضع التعلم: " + MODES[mode] + ". "
        + {"guided": "قدم شرحاً وخطوات عملية مع مثال.",
           "exercise": "قدم تمريناً واحداً، ثم انتظر إجابة المستخدم قبل شرح الحل.",
           "quiz": "اطرح سؤالاً واحداً دون كشف الإجابة، ثم قيّم إجابة المستخدم مع تفسير."}[mode]
    )


def bounded_history(history):
    # Last six pairs, at most 12k characters; never shared across users.
    result, remaining = [], 12000
    for row in reversed(history[-12:]):
        if len(row["content"]) > remaining:
            break
        result.insert(0, row)
        remaining -= len(row["content"])
    return result


def generate_reply(visitor_token, skill, mode, history, text, provider="gemini"):
    config = PROVIDERS[provider]
    access = ai_mode()
    if access == "disabled":
        raise GenerationFailure("خدمة AI غير مفعّلة على هذا الخادم بعد.", 503, "ai_disabled")
    if provider == "nvidia" and access != "promptql":
        raise GenerationFailure(
            "NVIDIA متاح فقط عبر بوابة PromptQL باتصال شخصي للزائر؛ الوضع المستقل يدعم Gemini مباشرة.",
            503, "nvidia_requires_gateway")
    instructions = learning_instructions(skill, mode)
    if provider == "nvidia":
        base = PROMPTQL_API_URL.rstrip("/")
        url = f'{base}/v1/integration/{config["id"]}/integrate.api.nvidia.com/v1/chat/completions'
        messages = [{"role": "system", "content": instructions}]
        messages += [{"role": r["role"], "content": r["content"]} for r in bounded_history(history)]
        messages.append({"role": "user", "content": text})
        body = {"model": config["model"], "messages": messages, "max_tokens": 512,
                "stream": False, "chat_template_kwargs": {"enable_thinking": False}}
    else:
        contents = [{"role": "model" if r["role"] == "assistant" else "user",
                     "parts": [{"text": r["content"]}]} for r in bounded_history(history)]
        contents.append({"role": "user", "parts": [{"text": text}]})
        body = {"systemInstruction": {"parts": [{"text": instructions}]},
                "contents": contents,
                "generationConfig": {"maxOutputTokens": 1800, "temperature": 0.65}}
        if access == "promptql":
            base = PROMPTQL_API_URL.rstrip("/")
            url = f'{base}/v1/integration/{config["id"]}/generativelanguage.googleapis.com/v1beta/models/{config["model"]}:generateContent'
        else:
            url = f'https://generativelanguage.googleapis.com/v1beta/models/{config["model"]}:generateContent'
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if access == "promptql":
        headers["Authorization"] = "Bearer " + visitor_token
        headers["X-PromptQL-Description"] = "Generate an Arabic Waha learning response with " + config["label"]
    else:
        headers["x-goog-api-key"] = GEMINI_API_KEY
    outbound = urllib.request.Request(url, method="POST", data=json.dumps(body).encode(),
                                      headers=headers)
    try:
        with urllib.request.urlopen(outbound, timeout=75) as result:
            response_body = result.read()
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            raise GenerationFailure("الوصول إلى " + config["label"] +
                " غير متاح. تحقق من مفتاح API أو اتصال حسابك وموافقة التطبيق؛ لا يتم التحويل لموفّر آخر.",
                403, "ai_permission")
        if error.code in (402, 429):
            delay = error.headers.get("Retry-After", "") if error.headers else ""
            wait = int(delay) if delay.isdigit() else DEFAULT_RETRY_AFTER_SECONDS
            wait = max(1, min(wait, AI_RETRY_AFTER_MAX_SECONDS))
            raise GenerationFailure("بلغت " + config["label"] +
                " حد الطلبات أو الحصة. انتظر وراجع حصة حسابك؛ لم يتم استخدام موفّر بديل.",
                429, "ai_rate_limit", wait)
        raise GenerationFailure("خدمة " + config["label"] + " غير متاحة حالياً. حاول لاحقاً.")
    except (TimeoutError, socket.timeout):
        raise GenerationFailure("انتهت مهلة " + config["label"] +
                ". لم تُحفظ رسالة ناقصة؛ حاول مرة أخرى.", 504, "ai_timeout")
    except (urllib.error.URLError, OSError):
        raise GenerationFailure("تعذّر الاتصال بخدمة AI. حاول مرة أخرى.")
    try:
        data = json.loads(response_body)
        if provider == "nvidia":
            reply = data["choices"][0]["message"].get("content", "")
            usage = data.get("usage", {})
            g.ai_usage = {k: v for k, v in usage.items()
                          if k in ("prompt_tokens", "completion_tokens") and isinstance(v, int)}
        else:
            reply = "".join(p.get("text", "") for p in data["candidates"][0]
                .get("content", {}).get("parts", []) if not p.get("thought"))
        if not isinstance(reply, str) or not reply.strip():
            raise ValueError()
    except (ValueError, IndexError, TypeError, KeyError):
        raise GenerationFailure("لم تُرجع خدمة AI نصاً صالحاً. عدّل سؤالك وحاول مرة أخرى.", 502, "ai_empty")
    return reply[:24000]


def cleanup_provider_cooldowns(db, now=None):
    if now is None:
        now = time.time()
    run(db, "DELETE FROM provider_cooldown WHERE until_time<=?", (now,))


def _cap_global_cooldown(db, provider, until_time, now):
    capped_until = min(until_time, now + GLOBAL_COOLDOWN_MAX_SECONDS)
    if capped_until < until_time:
        run(db, """UPDATE provider_cooldown SET until_time=?
                 WHERE provider=? AND user_id=? AND until_time=?""",
            (capped_until, provider, GLOBAL_COOLDOWN_USER_ID, until_time))
    return capped_until


def active_provider_cooldown(db, provider, user_id, now):
    """Return the active cooldown timestamp for this visitor or app-wide row."""
    cleanup_provider_cooldowns(db, now)
    rows = run(db, """SELECT user_id,until_time FROM provider_cooldown
                     WHERE provider=? AND user_id IN (?,?)""",
               (provider, user_id, GLOBAL_COOLDOWN_USER_ID)).fetchall()
    active_until = 0
    for row in rows:
        until_time = row["until_time"]
        if row["user_id"] == GLOBAL_COOLDOWN_USER_ID:
            until_time = _cap_global_cooldown(db, provider, until_time, now)
        if until_time > now:
            active_until = max(active_until, until_time)
    return active_until


def upsert_provider_cooldown(db, provider, user_id, until_time):
    if POSTGRES:
        cooldown_sql = """INSERT INTO provider_cooldown(provider,user_id,until_time) VALUES(?,?,?)
            ON CONFLICT(provider,user_id) DO UPDATE
            SET until_time=GREATEST(
                provider_cooldown.until_time, excluded.until_time)"""
    else:
        cooldown_sql = """INSERT INTO provider_cooldown(provider,user_id,until_time) VALUES(?,?,?)
            ON CONFLICT(provider,user_id) DO UPDATE
            SET until_time=MAX(provider_cooldown.until_time, excluded.until_time)"""
    run(db, cooldown_sql, (provider, user_id, until_time))


def upsert_nvidia_rate_limit_cooldowns(db, user_id, retry_after, now=None):
    """Store NVIDIA 429/402 cooldowns with both safety scopes.

    The visitor row keeps the full provider Retry-After, matching PromptQL's
    personal-connection path. The reserved global row is deliberately short so
    a possibly shared key/account gets a brief rest without reviving the old
    app-wide 24h lockout.
    """
    if now is None:
        now = time.time()
    cleanup_provider_cooldowns(db, now)
    upsert_provider_cooldown(db, "nvidia", user_id, now + retry_after)
    upsert_provider_cooldown(
        db, "nvidia", GLOBAL_COOLDOWN_USER_ID,
        now + min(retry_after, GLOBAL_COOLDOWN_MAX_SECONDS))
    global_row = run(db, "SELECT until_time FROM provider_cooldown WHERE provider=? AND user_id=?",
                     ("nvidia", GLOBAL_COOLDOWN_USER_ID)).fetchone()
    if global_row:
        _cap_global_cooldown(db, "nvidia", global_row["until_time"], now)


@app.post("/api/sessions/<sid>/message")
def message(sid):
    data = request.get_json(silent=True) or {}
    text = data.get("text")
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= 6000:
        return fail("اكتب رسالة من 1 إلى 6000 حرف.")
    text = text.strip()
    now, uid = time.time(), g.visitor["id"]
    ip_key = "ip:" + hashlib.sha256(client_ip().encode()).hexdigest()
    nvidia_attempt_id = None
    attempt_status = "failed"
    with connect() as db:
        row = owned_session(db, sid)
        if not row:
            return fail("المحادثة غير موجودة أو غير متاحة لك.", 404, "not_found")
        if row["pending_until"] > now:
            return fail("هناك رد قيد الإنشاء لهذه المحادثة.", 409, "busy")
        if "provider" in data and data["provider"] != row["provider"]:
            return fail("الموفّر ثابت لهذه الجلسة. ابدأ جلسة جديدة لتغيير الموفّر.", 400, "provider_immutable")
        if row["provider"] == "nvidia":
            cooldown_until = active_provider_cooldown(db, "nvidia", uid, now)
            if cooldown_until > now:
                response, status = fail("NVIDIA طلب الانتظار قبل إعادة المحاولة. لا تحويل تلقائي.", 429, "nvidia_cooldown")
                response.headers["Retry-After"] = str(max(1, int(cooldown_until - now)))
                return response, status
            minute = run(db, "SELECT COUNT(1) AS n FROM nvidia_attempts WHERE created_at>?",
                         (now - 60,)).fetchone()["n"]
            day = run(db, "SELECT COUNT(1) AS n FROM nvidia_attempts WHERE created_at>?",
                      (now - 86400,)).fetchone()["n"]
            if minute >= NVIDIA_PER_MINUTE or day >= NVIDIA_PER_DAY:
                return fail("حد NVIDIA التجريبي للتطبيق كله: 10 محاولات/دقيقة و100 خلال 24 ساعة. لا تحويل تلقائي.",
                            429, "nvidia_budget")
            active = run(db, "SELECT COUNT(1) AS n FROM sessions WHERE provider='nvidia' AND pending_until>?",
                         (now,)).fetchone()["n"]
            if active >= NVIDIA_MAX_ACTIVE:
                return fail("NVIDIA مشغول بطلبات أخرى. انتظر انتهاء أحدها.", 409, "nvidia_busy")
        user_count = run(db, "SELECT COUNT(1) AS n FROM attempts WHERE user_id=? AND created_at>?",
                         (uid, now - 3600)).fetchone()["n"]
        if user_count >= USER_AI_LIMIT_PER_HOUR:
            return fail("حد التجربة: 30 طلباً في الساعة لكل مستخدم.", 429, "local_rate_limit")
        ip_count = run(db, "SELECT COUNT(1) AS n FROM attempts WHERE user_id=? AND created_at>?",
                       (ip_key, now - 3600)).fetchone()["n"]
        if ip_count >= IP_AI_LIMIT_PER_HOUR:
            return fail("الحد المشترك من هذا العنوان بلغ حده. حاول لاحقاً.", 429, "local_rate_limit")
        claimed = run(db, "UPDATE sessions SET pending_until=? WHERE id=? AND user_id=? AND pending_until<=?",
                      (now + 100, sid, uid, now)).rowcount
        if not claimed:
            return fail("هناك رد قيد الإنشاء لهذه المحادثة.", 409, "busy")
        history = [dict(r) for r in run(
            db, "SELECT role,content FROM messages WHERE session_id=? ORDER BY id DESC LIMIT 12",
            (sid,)).fetchall()][::-1]
        run(db, "INSERT INTO attempts VALUES(?,?)", (uid, now))
        run(db, "INSERT INTO attempts VALUES(?,?)", (ip_key, now))
        run(db, "DELETE FROM attempts WHERE created_at<?", (now - 86400,))
        if row["provider"] == "nvidia":
            nvidia_attempt_id = insert_returning_id(
                db, "INSERT INTO nvidia_attempts(user_id,created_at) VALUES(?,?)", (uid, now))
            run(db, "DELETE FROM nvidia_attempts WHERE created_at<?", (now - 604800,))
    try:
        reply = generate_reply(g.visitor["token"], BY_ID[row["skill_id"]], row["mode"],
                               history, text, row["provider"])
        with connect() as db:
            run(db, "INSERT INTO messages(session_id,role,content,created_at,provider) VALUES(?,?,?,?,?)",
                (sid, "user", text, now, row["provider"]))
            run(db, "INSERT INTO messages(session_id,role,content,created_at,provider) VALUES(?,?,?,?,?)",
                (sid, "assistant", reply, time.time(), row["provider"]))
            title = text[:60] if not history else row["title"]
            run(db, "UPDATE sessions SET updated_at=?,title=? WHERE id=?", (time.time(), title, sid))
            result = session_view(db, owned_session(db, sid), True)
        attempt_status = "success"
        return jsonify(session=result)
    except GenerationFailure as error:
        attempt_status = error.code
        response, status = fail(error.message, error.status, error.code)
        if error.retry_after:
            response.headers["Retry-After"] = str(error.retry_after)
            if row["provider"] == "nvidia":
                with connect() as db:
                    upsert_nvidia_rate_limit_cooldowns(db, uid, error.retry_after)
        return response, status
    finally:
        with connect() as db:
            run(db, "UPDATE sessions SET pending_until=0 WHERE id=? AND user_id=?", (sid, uid))
            if nvidia_attempt_id is not None:
                usage = getattr(g, "ai_usage", {})
                run(db, """UPDATE nvidia_attempts SET status=?,elapsed_ms=?,prompt_tokens=?,
                           completion_tokens=? WHERE id=?""",
                    (attempt_status, int((time.time() - now) * 1000), usage.get("prompt_tokens"),
                     usage.get("completion_tokens"), nvidia_attempt_id))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5210, debug=False)
