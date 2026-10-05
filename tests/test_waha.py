"""Offline CI tests. Fake identities are limited to this test client only."""
import base64
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
sys.path.insert(0, str(ROOT / "scripts"))
from build_catalog import load_skills, validate

temp = tempfile.TemporaryDirectory()
os.environ["WAHA_DB"] = str(Path(temp.name) / "waha-test.db")
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
        response = self.client.post("/api/sessions", json={"skill_id": "SKL002",
            "mode": "execute_untrusted_code"}, headers=self.a)
        self.assertEqual(response.status_code, 400)

    def test_download_valid(self):
        response = self.client.get("/api/skills/SKL002/download")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["skill"]["id"], "SKL002")


if __name__ == "__main__":
    unittest.main()