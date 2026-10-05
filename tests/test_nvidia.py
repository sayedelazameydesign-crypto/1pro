"""Offline NVIDIA provider regression tests; no keys or real network."""
import time
import unittest
from unittest.mock import patch
from test_waha import backend, identity


class NvidiaTests(unittest.TestCase):
    def setUp(self):
        self.client = backend.app.test_client()
        with backend.connect() as db:
            for table in ["messages", "sessions", "attempts", "nvidia_attempts", "provider_cooldown"]:
                db.execute("DELETE FROM " + table)
        self.headers = self.auth_headers("nvidia-test")
        self.other_headers = self.auth_headers("nvidia-other")

    def auth_headers(self, user):
        headers = {"X-PromptQL-Visitor-Token": identity(user)}
        return {**headers, "X-Waha-CSRF": self.client.get("/api/me", headers=headers).get_json()["csrf"]}

    def create(self, confirm=True, headers=None):
        r = self.client.post("/api/sessions", json={"skill_id": "SKL002", "provider": "nvidia",
            "free_endpoint_confirmed": confirm}, headers=headers or self.headers)
        return r

    def send(self, sid, headers=None, **extra):
        return self.client.post("/api/sessions/" + sid + "/message", json={"text": "سؤال", **extra},
                                headers=headers or self.headers)

    def test_confirm_free_eligibility(self):
        self.assertEqual(self.create(False).status_code, 400)

    def test_provider_immutable(self):
        sid = self.create().get_json()["session"]["id"]
        self.assertEqual(self.send(sid, provider="gemini").status_code, 400)

    def test_provider_persisted(self):
        sid = self.create().get_json()["session"]["id"]
        with patch.object(backend, "generate_reply", return_value="رد تجريبي") as generate:
            r = self.send(sid)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(generate.call_args.args[-1], "nvidia")
        self.assertEqual(r.get_json()["session"]["messages"][-1]["provider"], "nvidia")

    def test_global_budget(self):
        sid = self.create().get_json()["session"]["id"]
        with backend.connect() as db:
            for _ in range(10):
                backend.run(db, "INSERT INTO nvidia_attempts(user_id,created_at,status) VALUES(?,?,?)",
                            ("another-user", time.time(), "failed"))
        with patch.object(backend, "generate_reply") as generate:
            r = self.send(sid)
        self.assertEqual(r.get_json()["code"], "nvidia_budget")
        generate.assert_not_called()

    def test_budget_rolling24h(self):
        sid = self.create().get_json()["session"]["id"]
        with backend.connect() as db:
            for _ in range(100):
                backend.run(db, "INSERT INTO nvidia_attempts(user_id,created_at,status) VALUES(?,?,?)",
                            ("another-user", time.time() - 120, "success"))
        self.assertEqual(self.send(sid).get_json()["code"], "nvidia_budget")

    def test_cooldown_no_fallback(self):
        sid = self.create().get_json()["session"]["id"]
        with patch.object(backend, "generate_reply",
                          side_effect=backend.GenerationFailure("Wait", 429, "ai_rate_limit", 90)) as generate:
            self.assertEqual(self.send(sid).headers["Retry-After"], "90")
            self.assertEqual(self.send(sid).get_json()["code"], "nvidia_cooldown")
            self.assertEqual(generate.call_count, 1)
        with backend.connect() as db:
            row = backend.run(db, "SELECT user_id,until_time FROM provider_cooldown WHERE provider=? AND user_id=?",
                              ("nvidia", "nvidia-test")).fetchone()
            global_row = backend.run(db, "SELECT until_time FROM provider_cooldown WHERE provider=? AND user_id=?",
                                     ("nvidia", backend.GLOBAL_COOLDOWN_USER_ID)).fetchone()
        self.assertEqual(row["user_id"], "nvidia-test")
        self.assertGreater(row["until_time"], time.time())
        self.assertIsNotNone(global_row)
        self.assertLessEqual(global_row["until_time"], time.time() + backend.GLOBAL_COOLDOWN_MAX_SECONDS)

    def test_personal_cooldown_does_not_lock_other_visitors_for_full_retry_after(self):
        sid_a = self.create(headers=self.headers).get_json()["session"]["id"]
        sid_b = self.create(headers=self.other_headers).get_json()["session"]["id"]
        with patch.object(backend, "generate_reply",
                          side_effect=backend.GenerationFailure("Wait", 429, "ai_rate_limit", 3600)) as generate:
            limited = self.send(sid_a, headers=self.headers)
            short_global = self.send(sid_b, headers=self.other_headers)
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(limited.headers["Retry-After"], "3600")
        self.assertEqual(short_global.get_json()["code"], "nvidia_cooldown")
        self.assertLessEqual(int(short_global.headers["Retry-After"]), backend.GLOBAL_COOLDOWN_MAX_SECONDS)
        self.assertEqual(generate.call_count, 1)

        with backend.connect() as db:
            backend.run(db, """UPDATE provider_cooldown SET until_time=?
                            WHERE provider=? AND user_id=?""",
                        (time.time() - 1, "nvidia", backend.GLOBAL_COOLDOWN_USER_ID))
        with patch.object(backend, "generate_reply", return_value="رد للزائر الآخر") as generate:
            other = self.send(sid_b, headers=self.other_headers)
            still_limited = self.send(sid_a, headers=self.headers)
        self.assertEqual(other.status_code, 200)
        self.assertEqual(other.get_json()["session"]["messages"][-1]["content"], "رد للزائر الآخر")
        self.assertEqual(still_limited.get_json()["code"], "nvidia_cooldown")
        self.assertGreater(int(still_limited.headers["Retry-After"]), backend.GLOBAL_COOLDOWN_MAX_SECONDS)
        self.assertEqual(generate.call_count, 1)

    def test_cooldown_upsert_sql_is_qualified_after_user_scope(self):
        class FakeCursor:
            def __init__(self):
                self.calls = []

            def execute(self, sql, params=()):
                self.calls.append((sql, params))
                return self

        original = backend.POSTGRES
        backend.POSTGRES = True
        try:
            cursor = FakeCursor()
            backend.upsert_provider_cooldown(cursor, "nvidia", "visitor", 123.0)
        finally:
            backend.POSTGRES = original
        sql, params = cursor.calls[0]
        self.assertIn("ON CONFLICT(provider,user_id)", sql)
        self.assertIn("provider_cooldown.until_time", sql)
        self.assertIn("excluded.until_time", sql)
        self.assertEqual(params, ("nvidia", "visitor", 123.0))

    def test_expired_cooldowns_are_cleaned_up(self):
        now = time.time()
        with backend.connect() as db:
            backend.upsert_provider_cooldown(db, "nvidia", "expired-visitor", now - 10)
            self.assertEqual(backend.active_provider_cooldown(db, "nvidia", "expired-visitor", now), 0)
            row = backend.run(db, "SELECT 1 FROM provider_cooldown WHERE provider=? AND user_id=?",
                              ("nvidia", "expired-visitor")).fetchone()
        self.assertIsNone(row)

    def test_global_cooldown_row_is_capped_to_short_app_wide_wait(self):
        sid = self.create().get_json()["session"]["id"]
        far_future = time.time() + 3600
        with backend.connect() as db:
            backend.upsert_provider_cooldown(db, "nvidia", backend.GLOBAL_COOLDOWN_USER_ID, far_future)
        response = self.send(sid)
        self.assertEqual(response.get_json()["code"], "nvidia_cooldown")
        self.assertLessEqual(int(response.headers["Retry-After"]), backend.GLOBAL_COOLDOWN_MAX_SECONDS)
        with backend.connect() as db:
            row = backend.run(db, "SELECT until_time FROM provider_cooldown WHERE provider=? AND user_id=?",
                              ("nvidia", backend.GLOBAL_COOLDOWN_USER_ID)).fetchone()
        self.assertLessEqual(row["until_time"], time.time() + backend.GLOBAL_COOLDOWN_MAX_SECONDS)

    def test_old_provider_cooldown_migration_preserves_global_row(self):
        old_until = time.time() + 120
        with backend.connect() as db:
            db.execute("DROP TABLE provider_cooldown")
            if backend.POSTGRES:
                db.execute("CREATE TABLE provider_cooldown(provider TEXT PRIMARY KEY, until_time DOUBLE PRECISION NOT NULL)")
            else:
                db.execute("CREATE TABLE provider_cooldown(provider TEXT PRIMARY KEY, until_time REAL NOT NULL)")
            backend.run(db, "INSERT INTO provider_cooldown(provider,until_time) VALUES(?,?)",
                        ("nvidia", old_until))
        backend.initialize()
        with backend.connect() as db:
            row = backend.run(db, "SELECT provider,user_id,until_time FROM provider_cooldown").fetchone()
            if backend.POSTGRES:
                pk_columns = backend._postgres_primary_key_columns(db, "provider_cooldown")
            else:
                columns = db.execute("PRAGMA table_info(provider_cooldown)").fetchall()
                pk_columns = [c[1] for c in sorted((c for c in columns if c[5]), key=lambda c: c[5])]
        self.assertEqual(pk_columns, ["provider", "user_id"])
        self.assertEqual(row["provider"], "nvidia")
        self.assertEqual(row["user_id"], backend.GLOBAL_COOLDOWN_USER_ID)
        self.assertAlmostEqual(row["until_time"], old_until, delta=2)

    def test_context_bounded(self):
        rows = backend.bounded_history([{"role": "user", "content": "x" * 5000}] * 30)
        self.assertLessEqual(len(rows), 12)
        self.assertLessEqual(sum(len(r["content"]) for r in rows), 12000)

    def test_failed_generation_no_partial(self):
        sid = self.create().get_json()["session"]["id"]
        with patch.object(backend, "generate_reply", side_effect=backend.GenerationFailure("Fail", 502)):
            self.assertEqual(self.send(sid).status_code, 502)
        self.assertEqual(self.client.get("/api/sessions/" + sid, headers=self.headers).get_json()["session"]["messages"], [])
        with backend.connect() as db:
            self.assertEqual(db.execute("SELECT status FROM nvidia_attempts").fetchone()["status"], "ai_unavailable")
