"""Persistence for Marketplace installations, resources and webhook events.

Written against the same portable conventions as the rest of the backend --
``?`` placeholders translated by ``app.run``, SQLite upserts, and a tiny adapter
(``connect`` / ``run`` / ``insert_returning_id`` / ``postgres``) handed in by
``app.py`` -- so this module imports in a test with neither Flask nor Postgres.

Two rules shape every function here:

**No view carries a credential.** Vercel's installation access token and the
resource's API token are *why* this data exists, so they are stored -- but
``public_installation`` / ``public_resource`` replace them with a fingerprint
before anything leaves the module. A dashboard that can display a token can leak
one; this repository's answer to that has consistently been a fingerprint and a
rotation endpoint instead.

**Encryption at rest is the operator's job, and the code says so.** Waha runs on
managed Postgres (Neon) or on a Render disk, both of which encrypt volumes.
Faking application-level encryption with a homebrew keystream would be worse
than none, because it would *look* solved. The mitigation that is real is
rotation: ``rotate_resource_token`` exists so a leaked token has a short life,
and the injected variables are replaced in one call rather than by hand.
"""
from __future__ import annotations

import json
import time
from typing import Mapping, Optional, Sequence

ACTIVE_STATUSES = ("active",)
RESOURCE_ACTIVE = ("ready", "pending", "onboarding", "suspended", "resumed")
RESOURCE_TERMINAL = ("error", "uninstalled")

SQLITE_TABLES = """
CREATE TABLE IF NOT EXISTS mp_installations(
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL DEFAULT '',
    account_name TEXT NOT NULL DEFAULT '',
    contact_email TEXT NOT NULL DEFAULT '',
    account_url TEXT NOT NULL DEFAULT '',
    team_id TEXT NOT NULL DEFAULT '',
    scopes TEXT NOT NULL DEFAULT '[]',
    accepted_policies TEXT NOT NULL DEFAULT '{}',
    access_token TEXT NOT NULL DEFAULT '',
    token_type TEXT NOT NULL DEFAULT 'Bearer',
    billing_plan_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS mp_resources(
    id TEXT PRIMARY KEY,
    installation_id TEXT NOT NULL,
    product_id TEXT NOT NULL,
    name TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'ready',
    billing_plan_id TEXT NOT NULL DEFAULT '',
    workspace_id TEXT NOT NULL DEFAULT '',
    api_base TEXT NOT NULL DEFAULT '',
    api_token TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS mp_events(
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT '',
    installation_id TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL DEFAULT '{}',
    handled TEXT NOT NULL DEFAULT '',
    received_at REAL NOT NULL);
"""

SQLITE_INDICES = """
CREATE INDEX IF NOT EXISTS mp_resources_installation
    ON mp_resources(installation_id, updated_at);
CREATE INDEX IF NOT EXISTS mp_resources_workspace ON mp_resources(workspace_id);
CREATE INDEX IF NOT EXISTS mp_events_received ON mp_events(received_at);
"""

PG_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS mp_installations(
        id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL DEFAULT '',
        account_name TEXT NOT NULL DEFAULT '',
        contact_email TEXT NOT NULL DEFAULT '',
        account_url TEXT NOT NULL DEFAULT '',
        team_id TEXT NOT NULL DEFAULT '',
        scopes TEXT NOT NULL DEFAULT '[]',
        accepted_policies TEXT NOT NULL DEFAULT '{}',
        access_token TEXT NOT NULL DEFAULT '',
        token_type TEXT NOT NULL DEFAULT 'Bearer',
        billing_plan_id TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'active',
        created_at DOUBLE PRECISION NOT NULL,
        updated_at DOUBLE PRECISION NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS mp_resources(
        id TEXT PRIMARY KEY,
        installation_id TEXT NOT NULL,
        product_id TEXT NOT NULL,
        name TEXT NOT NULL,
        metadata TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'ready',
        billing_plan_id TEXT NOT NULL DEFAULT '',
        workspace_id TEXT NOT NULL DEFAULT '',
        api_base TEXT NOT NULL DEFAULT '',
        api_token TEXT NOT NULL DEFAULT '',
        created_at DOUBLE PRECISION NOT NULL,
        updated_at DOUBLE PRECISION NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS mp_resources_installation"
    " ON mp_resources(installation_id, updated_at)",
    "CREATE INDEX IF NOT EXISTS mp_resources_workspace ON mp_resources(workspace_id)",
    """CREATE TABLE IF NOT EXISTS mp_events(
        id TEXT PRIMARY KEY,
        type TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT '',
        installation_id TEXT NOT NULL DEFAULT '',
        payload TEXT NOT NULL DEFAULT '{}',
        handled TEXT NOT NULL DEFAULT '',
        received_at DOUBLE PRECISION NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS mp_events_received ON mp_events(received_at)",
]

SCHEMA_STATEMENTS = (
    tuple(statement.strip() for statement in SQLITE_TABLES.strip().split(";\n") if statement.strip())
    + tuple(statement.strip() for statement in SQLITE_INDICES.strip().split(";\n") if statement.strip())
)


def _fingerprint(value: str) -> str:
    import hashlib

    if not value:
        return ""
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"{digest[:4]}…{digest[-4:]}"


def _dump(value) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return "{}"


def _load(raw, default):
    try:
        value = json.loads(raw) if raw else default
    except (ValueError, TypeError):
        return default
    return value if isinstance(value, type(default)) else default


class MarketplaceStore:
    """The only object that knows SQL for the Marketplace surface."""

    def __init__(self, db, clock=time.time):
        self.db = db
        self.clock = clock

    # -- schema ------------------------------------------------------------
    def ensure_schema(self):
        """Additive, idempotent schema creation for whichever engine is active."""
        statements = PG_SCHEMA if self.db.postgres else SCHEMA_STATEMENTS
        with self.db.connect() as conn:
            for statement in statements:
                self.db.run(conn, statement)

    # -- installations -----------------------------------------------------
    def upsert_installation(self, installation_id, *, access_token="", token_type="Bearer",
                            scopes=None, accepted_policies=None, account=None, team_id="",
                            billing_plan_id=""):
        """Create or refresh one installation.

        Re-installing is not an error: Vercel replays ``PUT`` when a customer
        changes scopes, and a fresh install over a stale row must take the new
        token rather than keep the old one, because only the newest token works.
        """
        now = self.clock()
        account = account if isinstance(account, Mapping) else {}
        contact = account.get("contact") if isinstance(account.get("contact"), Mapping) else {}
        shared = (str(account.get("account_id") or ""),
                  str(account.get("name") or ""),
                  str(contact.get("email") or ""),
                  str(account.get("url") or ""),
                  str(team_id or ""),
                  _dump(list(scopes or [])),
                  _dump(dict(accepted_policies or {})),
                  str(access_token or ""),
                  str(token_type or "Bearer"),
                  str(billing_plan_id or ""),
                  "active",
                  now)
        # UPDATE-then-INSERT rather than an engine-specific upsert: `app.run`
        # only rewrites `INSERT OR IGNORE`, and this stays identical on SQLite
        # and Postgres. A returned rowcount of zero is the only signal both
        # drivers give for "not there yet".
        with self.db.connect() as conn:
            cursor = self.db.run(conn, """UPDATE mp_installations SET account_id=?,
                account_name=?, contact_email=?, account_url=?, team_id=?, scopes=?,
                accepted_policies=?, access_token=?, token_type=?, billing_plan_id=?,
                status=?, updated_at=? WHERE id=?""", (*shared, installation_id))
            if not getattr(cursor, "rowcount", 0):
                self.db.run(conn, """INSERT INTO mp_installations(id,account_id,account_name,
                    contact_email,account_url,team_id,scopes,accepted_policies,access_token,
                    token_type,billing_plan_id,status,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (installation_id, *shared, now))
        return self.get_installation(installation_id)

    def get_installation(self, installation_id):
        with self.db.connect() as conn:
            row = self.db.run(conn, "SELECT * FROM mp_installations WHERE id=?",
                              (installation_id,)).fetchone()
        return self.public_installation(dict(row)) if row is not None else None

    def raw_installation(self, installation_id):
        """Full row, including the access token. Only for outbound Vercel calls."""
        with self.db.connect() as conn:
            row = self.db.run(conn, "SELECT * FROM mp_installations WHERE id=?",
                              (installation_id,)).fetchone()
        return dict(row) if row is not None else None

    def list_installations(self, limit=50):
        with self.db.connect() as conn:
            rows = self.db.run(conn, """SELECT * FROM mp_installations
                                        ORDER BY updated_at DESC LIMIT ?""",
                               (max(1, min(200, int(limit))),)).fetchall()
        return [self.public_installation(dict(row)) for row in rows]

    def mark_uninstalled(self, installation_id):
        """Record the removal and retire every resource under it.

        The token is cleared rather than kept: a removed integration's token
        stops working at Vercel the moment the configuration is deleted, so a
        copy here would be a stale credential with no owner -- and deleting the
        row would lose the audit trail of what was provisioned.
        """
        now = self.clock()
        with self.db.connect() as conn:
            self.db.run(conn, """UPDATE mp_installations SET status='uninstalled',
                                 access_token='', updated_at=? WHERE id=?""",
                        (now, installation_id))
            self.db.run(conn, """UPDATE mp_resources SET status='uninstalled', api_token='',
                                 updated_at=? WHERE installation_id=?""",
                        (now, installation_id))

    # -- resources ---------------------------------------------------------
    def count_resources(self, installation_id, exclude=None):
        sql = "SELECT COUNT(1) AS n FROM mp_resources WHERE installation_id=?"
        params = [installation_id]
        if exclude:
            sql += " AND id<>?"
            params.append(exclude)
        with self.db.connect() as conn:
            row = self.db.run(conn, sql, params).fetchone()
        return int(row["n"]) if row is not None else 0

    def token_for_workspace(self, workspace_id):
        """The current API token for a workspace, or "".

        Read on every request that presents a workspace credential. It is one
        indexed lookup, and it is what makes rotation an actual revocation: a
        signed token cannot be un-signed, so the comparison against the stored
        value is the only place "still current" is decided.
        """
        if not workspace_id:
            return ""
        with self.db.connect() as conn:
            row = self.db.run(conn, "SELECT api_token FROM mp_resources WHERE workspace_id=?",
                              (workspace_id,)).fetchone()
        return (row["api_token"] if row is not None else "") or ""

    def count_all_resources(self):
        """Total resources across every installation. Counted in SQL, not in Python."""
        with self.db.connect() as conn:
            row = self.db.run(conn, "SELECT COUNT(1) AS n FROM mp_resources").fetchone()
        return int(row["n"]) if row is not None else 0

    def create_resource(self, resource_id, installation_id, product_id, name, metadata,
                        billing_plan_id, workspace_id, api_base, api_token, status="ready"):
        now = self.clock()
        with self.db.connect() as conn:
            self.db.run(conn, """INSERT INTO mp_resources(id,installation_id,product_id,name,
                metadata,status,billing_plan_id,workspace_id,api_base,api_token,created_at,
                updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (resource_id, installation_id, product_id, name, _dump(metadata or {}),
                         status, billing_plan_id, workspace_id, api_base, api_token, now, now))
        return self.get_resource(installation_id, resource_id)

    def get_resource(self, installation_id, resource_id):
        with self.db.connect() as conn:
            row = self.db.run(conn, """SELECT * FROM mp_resources
                                       WHERE installation_id=? AND id=?""",
                              (installation_id, resource_id)).fetchone()
        return self.public_resource(dict(row)) if row is not None else None

    def raw_resource(self, installation_id, resource_id):
        """Full row, token included. Only for rotation and injection."""
        with self.db.connect() as conn:
            row = self.db.run(conn, """SELECT * FROM mp_resources
                                       WHERE installation_id=? AND id=?""",
                              (installation_id, resource_id)).fetchone()
        return dict(row) if row is not None else None

    def list_resources(self, installation_id, ids: Optional[Sequence[str]] = None):
        sql = "SELECT * FROM mp_resources WHERE installation_id=?"
        params = [installation_id]
        if ids:
            placeholders = ",".join("?" for _ in ids)
            sql += f" AND id IN ({placeholders})"
            params.extend(ids)
        sql += " ORDER BY updated_at DESC"
        with self.db.connect() as conn:
            rows = self.db.run(conn, sql, params).fetchall()
        return [self.public_resource(dict(row)) for row in rows]

    def update_resource(self, installation_id, resource_id, *, name=None, metadata=None,
                        status=None, billing_plan_id=None):
        row = self.raw_resource(installation_id, resource_id)
        if row is None:
            return None
        fields = []
        params = []
        for column, value in (("name", name), ("status", status),
                              ("billing_plan_id", billing_plan_id)):
            if value is not None:
                fields.append(f"{column}=?")
                params.append(value)
        if metadata is not None:
            fields.append("metadata=?")
            params.append(_dump(metadata))
        if not fields:
            return self.get_resource(installation_id, resource_id)
        fields.append("updated_at=?")
        params.append(self.clock())
        params.extend((installation_id, resource_id))
        with self.db.connect() as conn:
            self.db.run(conn, f"UPDATE mp_resources SET {', '.join(fields)}"
                              " WHERE installation_id=? AND id=?", params)
        return self.get_resource(installation_id, resource_id)

    def rotate_resource_token(self, installation_id, resource_id, api_token):
        row = self.raw_resource(installation_id, resource_id)
        if row is None:
            return None
        with self.db.connect() as conn:
            self.db.run(conn, """UPDATE mp_resources SET api_token=?, updated_at=?
                                 WHERE installation_id=? AND id=?""",
                        (api_token, self.clock(), installation_id, resource_id))
        return self.raw_resource(installation_id, resource_id)

    def delete_resource(self, installation_id, resource_id):
        with self.db.connect() as conn:
            cursor = self.db.run(conn, "DELETE FROM mp_resources WHERE installation_id=? AND id=?",
                                 (installation_id, resource_id))
        return bool(getattr(cursor, "rowcount", 0))

    # -- webhook events ----------------------------------------------------
    def record_event(self, event_id, event_type, *, created_at="", installation_id="",
                     payload=None, handled=""):
        """Store one event. Returns False when Vercel already sent it.

        Vercel retries a webhook until it gets a 2xx, so the same ``id`` arrives
        more than once. Recording it once is what keeps an uninstall from being
        applied twice, and the id is the primary key so the database enforces it
        rather than our memory of the last one.
        """
        if not event_id:
            return True
        now = self.clock()
        with self.db.connect() as conn:
            cursor = self.db.run(conn, """INSERT OR IGNORE INTO mp_events(id,type,created_at,
                installation_id,payload,handled,received_at) VALUES(?,?,?,?,?,?,?)""",
                                 (str(event_id), str(event_type or "unknown"),
                                  str(created_at or ""), str(installation_id or ""),
                                  _dump(payload if payload is not None else {}),
                                  str(handled or ""), now))
        return bool(getattr(cursor, "rowcount", 1))

    def mark_event_handled(self, event_id, handled):
        if not event_id:
            return
        with self.db.connect() as conn:
            self.db.run(conn, "UPDATE mp_events SET handled=? WHERE id=?",
                        (str(handled), str(event_id)))

    def list_events(self, limit=25):
        with self.db.connect() as conn:
            rows = self.db.run(conn, """SELECT * FROM mp_events
                                        ORDER BY received_at DESC LIMIT ?""",
                               (max(1, min(200, int(limit))),)).fetchall()
        return [{"id": row["id"], "type": row["type"], "created_at": row["created_at"],
                 "installation_id": row["installation_id"],
                 "payload": _load(row["payload"], {}),
                 "handled": row["handled"], "received_at": row["received_at"]}
                for row in rows]

    # -- public views ------------------------------------------------------
    @staticmethod
    def public_installation(row: Mapping) -> dict:
        return {
            "id": row.get("id"),
            "account_id": row.get("account_id") or "",
            "account_name": row.get("account_name") or "",
            "contact_email": row.get("contact_email") or "",
            "account_url": row.get("account_url") or "",
            "team_id": row.get("team_id") or "",
            "scopes": _load(row.get("scopes"), []),
            "accepted_policies": _load(row.get("accepted_policies"), {}),
            "token_type": row.get("token_type") or "Bearer",
            "billing_plan_id": row.get("billing_plan_id") or "",
            "status": row.get("status") or "active",
            "token_fingerprint": _fingerprint(row.get("access_token") or ""),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
        }

    @staticmethod
    def public_resource(row: Mapping) -> dict:
        """The shape the Partner API and the dashboard both send. No token."""
        return {
            "id": row.get("id"),
            "productId": row.get("product_id") or "",
            "name": row.get("name") or "",
            "metadata": _load(row.get("metadata"), {}),
            "status": row.get("status") or "ready",
            "billingPlanId": row.get("billing_plan_id") or "",
            "workspaceId": row.get("workspace_id") or "",
            "installationId": row.get("installation_id") or "",
            "tokenFingerprint": _fingerprint(row.get("api_token") or ""),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
        }

    def installation_summary(self, installation_id):
        """Installation plus its resources, for the dashboard and the API."""
        installation = self.get_installation(installation_id)
        if installation is None:
            return None
        installation = dict(installation)
        installation["resources"] = self.list_resources(installation_id)
        installation["resource_count"] = len(installation["resources"])
        return installation
