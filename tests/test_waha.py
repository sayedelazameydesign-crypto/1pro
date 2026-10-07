"""Offline CI tests. Fake identities are limited to this test client only."""
import base64
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from build_catalog import load_skills, validate

temp = tempfile.TemporaryDirectory()
os.environ["WAHA_DB"] = str(Path(temp.name) / "waha-test.db")
os.environ["WAHA_TRUST_PROMPTQL"] = "1"
os.environ["WAHA_ALLOWED_ORIGINS"] = "https://pages.test"
# Deterministic isolation: the "no keys" tests must stay true even when another
# test module (or the developer's shell) exported AI credentials.
for _key in ("GEMINI_API_KEY", "PROMPTQL_PLATFORM_API_URL", "WAHA_MODEL", "AGENT_MODEL"):
    os.environ.pop(_key, None)
spec = importlib.util.spec_from_file_location("waha_backend", ROOT / "backend/app.py")
backend = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backend)


def identity(user):
    value = base64.urlsafe_b64encode(json.dumps({"sub": user, "exp": time.time() + 600}).encode())
    return "test." + value.decode().rstrip("=") + ".test"


class CatalogTests(unittest.TestCase):
    def test_catalog_valid(self):
        self.assertGreaterEqual(len(load_skills()), 6)

    def test_duplicate_id_rejected(self):
        skill = load_skills()[0]
        with self.assertRaises(ValueError):
            validate(skill, {skill["id"]})

    def test_path_traversal_rejected(self):
        skill = dict(load_skills()[0], id="../escape")
        with self.assertRaises(ValueError):
            validate(skill, set())

    def test_unknown_fields_rejected(self):
        skill = dict(load_skills()[0], code="print('untrusted')")
        with self.assertRaises(ValueError):
            validate(skill, set())

    def test_missing_prompt_rejected(self):
        skill = dict(load_skills()[0])
        del skill["prompt"]
        with self.assertRaises(ValueError):
            validate(skill, set())

    def test_empty_prompt_rejected(self):
        with self.assertRaises(ValueError):
            validate(dict(load_skills()[0], prompt=""), set())


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.client = backend.app.test_client()
        with backend.connect() as db:
            for table in ["messages", "sessions", "installs", "attempts"]:
                db.execute(f"DELETE FROM {table}")
        self.a = self.headers("reviewer-a")
        self.b = self.headers("reviewer-b")

    def headers(self, user):
        headers = {"X-PromptQL-Visitor-Token": identity(user)}
        me = self.client.get("/api/me", headers=headers).get_json()
        return {**headers, "X-Waha-CSRF": me["csrf"]}

    def create(self):
        response = self.client.post("/api/sessions",
            json={"skill_id": "SKL002", "mode": "guided"}, headers=self.a)
        self.assertEqual(response.status_code, 201)
        return response.get_json()["session"]["id"]

    def test_readiness(self):
        self.assertEqual(self.client.get("/readyz").status_code, 204)

    def test_anonymous_no_write(self):
        self.assertEqual(self.client.post("/api/sessions",
            json={"skill_id": "SKL002"}).status_code, 401)

    def test_csrf_required(self):
        headers = {"X-PromptQL-Visitor-Token": self.a["X-PromptQL-Visitor-Token"]}
        self.assertEqual(self.client.post("/api/sessions",
            json={"skill_id": "SKL002"}, headers=headers).status_code, 403)

    def test_origin_rejected(self):
        self.assertEqual(self.client.post("/api/sessions", json={"skill_id": "SKL002"},
            headers={**self.a, "Origin": "https://malicious.invalid"}).status_code, 403)

    def test_saved_messages_and_isolation(self):
        sid = self.create()
        self.assertEqual(self.client.get(f"/api/sessions/{sid}", headers=self.b).status_code, 404)
        self.assertEqual(self.client.get(f"/api/sessions/{sid}/export", headers=self.b).status_code, 404)
        with patch.object(backend, "generate_reply", return_value="رد اختبار محلي فقط.") as generate:
            response = self.client.post(f"/api/sessions/{sid}/message",
                json={"text": "سؤال اختبار"}, headers=self.a)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(generate.call_args.args[0], self.a["X-PromptQL-Visitor-Token"])
        messages = self.client.get(f"/api/sessions/{sid}", headers=self.a).get_json()["session"]["messages"]
        self.assertEqual(len(messages), 2)
        self.assertEqual(self.client.post(f"/api/sessions/{sid}/delete",
            json={}, headers=self.b).status_code, 404)
        self.assertEqual(self.client.post(f"/api/sessions/{sid}/delete",
            json={}, headers=self.a).status_code, 200)
        self.assertEqual(self.client.get(f"/api/sessions/{sid}", headers=self.a).status_code, 404)

    def test_failed_generation_no_partial_save(self):
        sid = self.create()
        with patch.object(backend, "generate_reply",
                          side_effect=backend.GenerationFailure("test error", 429)):
            response = self.client.post(f"/api/sessions/{sid}/message",
                json={"text": "سؤال"}, headers=self.a)
        self.assertEqual(response.status_code, 429)
        session = self.client.get(f"/api/sessions/{sid}", headers=self.a).get_json()["session"]
        self.assertEqual(session["messages"], [])

    def test_invalid_mode(self):
        for mode in ("execute_untrusted_code", "chat"):
            response = self.client.post("/api/sessions", json={"skill_id": "SKL002",
                "mode": mode}, headers=self.a)
            self.assertEqual(response.status_code, 400)

    def test_general_assistant_chat_is_saved_and_uses_general_instructions(self):
        response = self.client.post("/api/sessions", json={"skill_id": "assistant"},
                                    headers=self.a)
        self.assertEqual(response.status_code, 201, response.get_json())
        session = response.get_json()["session"]
        self.assertEqual(session["skill_name"], "مساعدك الذكي")
        self.assertEqual(session["mode"], "chat")
        self.assertEqual(session["messages"], [])
        self.assertEqual([s["id"] for s in self.client.get(
            "/api/skills", headers=self.a).get_json()["skills"]].count("assistant"), 0)
        with patch.object(backend, "generate_reply", return_value="مرحباً! أنا جاهز للمساعدة.") as generate:
            result = self.client.post(f"/api/sessions/{session['id']}/message",
                json={"text": "ساعدني في ترتيب يومي"}, headers=self.a)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(generate.call_args.args[1]["id"], "assistant")
        self.assertEqual(generate.call_args.args[2], "chat")
        self.assertIn("لا تدّع الوصول إلى جهاز المستخدم",
                      backend.learning_instructions(backend.ASSISTANT_SKILL, "chat"))
        saved = self.client.get(f"/api/sessions/{session['id']}",
                                headers=self.a).get_json()["session"]
        self.assertEqual([m["role"] for m in saved["messages"]], ["user", "assistant"])
        with backend.connect() as db:
            installs = backend.run(db, "SELECT COUNT(1) AS n FROM installs WHERE user_id=?",
                                   ("reviewer-a",)).fetchone()["n"]
        self.assertEqual(installs, 0)

    def test_download_valid(self):
        response = self.client.get("/api/skills/SKL002/download")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["skill"]["id"], "SKL002")


class StandaloneTests(unittest.TestCase):
    """Standalone deployment mode: backend-issued tokens, CORS, direct AI off."""

    def setUp(self):
        self.client = backend.app.test_client()
        with backend.connect() as db:
            for table in ["messages", "sessions", "installs", "attempts"]:
                db.execute(f"DELETE FROM {table}")

    def register(self):
        response = self.client.post("/api/register", json={})
        self.assertEqual(response.status_code, 201)
        return response.get_json()

    def auth_headers(self, data):
        return {"Authorization": "Bearer " + data["token"], "X-Waha-CSRF": data["csrf"]}

    def test_health_shallow(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["database"], "postgres" if backend.POSTGRES else "sqlite")

    def test_register_and_session_flow(self):
        data = self.register()
        headers = self.auth_headers(data)
        response = self.client.post("/api/sessions",
            json={"skill_id": "SKL003", "mode": "quiz"}, headers=headers)
        self.assertEqual(response.status_code, 201)
        sid = response.get_json()["session"]["id"]
        listed = self.client.get("/api/sessions", headers=headers).get_json()
        self.assertEqual([s["id"] for s in listed["sessions"]], [sid])
        self.assertEqual(listed["installed_count"], 1)
        other = self.register()
        self.assertEqual(self.client.get(f"/api/sessions/{sid}",
            headers=self.auth_headers(other)).status_code, 404)

    def test_tampered_token_rejected(self):
        data = self.register()
        tampered = data["token"][:-1] + ("0" if data["token"][-1] != "0" else "1")
        headers = {"Authorization": "Bearer " + tampered, "X-Waha-CSRF": data["csrf"]}
        self.assertEqual(self.client.post("/api/sessions",
            json={"skill_id": "SKL002"}, headers=headers).status_code, 401)
        self.assertEqual(self.client.get("/api/sessions",
            headers={"Authorization": "Bearer " + tampered}).get_json()["sessions"], [])

    def test_register_rate_limit(self):
        for _ in range(backend.REGISTER_LIMIT_PER_HOUR):
            self.assertEqual(self.client.post("/api/register", json={}).status_code, 201)
        self.assertEqual(self.client.post("/api/register", json={}).status_code, 429)

    def test_cors_preflight_allowed_origin(self):
        response = self.client.options("/api/sessions", headers={"Origin": "https://pages.test"})
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.headers.get("Access-Control-Allow-Origin"), "https://pages.test")
        self.assertIn("X-Waha-CSRF", response.headers.get("Access-Control-Allow-Headers", ""))

    def test_cors_not_echoed_for_evil_origin(self):
        response = self.client.get("/api/skills", headers={"Origin": "https://evil.invalid"})
        self.assertNotIn("Access-Control-Allow-Origin", response.headers)

    def test_rate_limit_without_retry_after_uses_default(self):
        """A 429 with no Retry-After header must still produce a bounded wait."""
        data = self.register()
        headers = self.auth_headers(data)
        sid = self.client.post("/api/sessions", json={"skill_id": "SKL002",
            "mode": "guided"}, headers=headers).get_json()["session"]["id"]
        error = urllib.error.HTTPError("https://example.invalid", 429,
                                       "Too Many Requests", None, None)
        with patch.object(backend, "GEMINI_API_KEY", "test-only-key"), \
                patch.object(backend.urllib.request, "urlopen", side_effect=error):
            response = self.client.post(f"/api/sessions/{sid}/message",
                json={"text": "مرحبا"}, headers=headers)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.get_json()["code"], "ai_rate_limit")
        self.assertEqual(response.headers.get("Retry-After"),
                         str(backend.DEFAULT_RETRY_AFTER_SECONDS))

    def test_ai_disabled_without_keys(self):
        data = self.register()
        headers = self.auth_headers(data)
        sid = self.client.post("/api/sessions", json={"skill_id": "SKL002",
            "mode": "guided"}, headers=headers).get_json()["session"]["id"]
        response = self.client.post(f"/api/sessions/{sid}/message",
            json={"text": "مرحبا"}, headers=headers)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["code"], "ai_disabled")

    def test_postgres_sql_translation(self):
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
            backend.run(cursor, "INSERT OR IGNORE INTO installs VALUES(?,?,?)", ("u", "s", 1.0))
            backend.run(cursor, "SELECT COUNT(1) AS n FROM attempts WHERE user_id=? AND created_at>?",
                        ("u", 0.0))
        finally:
            backend.POSTGRES = original
        self.assertEqual(cursor.calls[0][0],
                         "INSERT INTO installs VALUES(%s,%s,%s) ON CONFLICT DO NOTHING")
        self.assertEqual(cursor.calls[0][1], ("u", "s", 1.0))
        self.assertNotIn("?", cursor.calls[1][0])
        self.assertIn("%s", cursor.calls[1][0])


if __name__ == "__main__":
    unittest.main()