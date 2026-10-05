"""Offline NVIDIA provider regression tests; no keys or real network."""
import json
import time
import unittest
import urllib.error
from unittest.mock import patch
from test_waha import backend, identity

class NvidiaTests(unittest.TestCase):
    def setUp(self):
        self.client=backend.app.test_client()
        with backend.connect() as db:
            for table in ["messages","sessions","attempts","nvidia_attempts","provider_cooldown"]:
                db.execute("DELETE FROM "+table)
        h={"X-PromptQL-Visitor-Token":identity("nvidia-test")}
        self.headers={**h,"X-Waha-CSRF":self.client.get("/api/me",headers=h).get_json()["csrf"]}
    def create(self,confirm=True):
        r=self.client.post("/api/sessions",json={"skill_id":"SKL002","provider":"nvidia",
            "free_endpoint_confirmed":confirm},headers=self.headers)
        return r
    def send(self,sid,**extra):
        return self.client.post("/api/sessions/"+sid+"/message",json={"text":"سؤال",**extra},
                                headers=self.headers)
    def test_confirm_free_eligibility(self):
        self.assertEqual(self.create(False).status_code,400)
    def test_provider_immutable(self):
        sid=self.create().get_json()["session"]["id"]
        self.assertEqual(self.send(sid,provider="gemini").status_code,400)
    def test_provider_persisted(self):
        sid=self.create().get_json()["session"]["id"]
        with patch.object(backend,"generate_reply",return_value="رد تجريبي") as generate:
            r=self.send(sid)
        self.assertEqual(r.status_code,200)
        self.assertEqual(generate.call_args.args[-1],"nvidia")
        self.assertEqual(r.get_json()["session"]["messages"][-1]["provider"],"nvidia")
    def test_global_budget(self):
        sid=self.create().get_json()["session"]["id"]
        with backend.connect() as db:
            for _ in range(10):
                backend.run(db, "INSERT INTO nvidia_attempts(user_id,created_at,status) VALUES(?,?,?)",
                            ("another-user", time.time(), "failed"))
        with patch.object(backend,"generate_reply") as generate:
            r=self.send(sid)
        self.assertEqual(r.get_json()["code"],"nvidia_budget")
        generate.assert_not_called()
    def test_budget_rolling24h(self):
        sid=self.create().get_json()["session"]["id"]
        with backend.connect() as db:
            for _ in range(100):
                backend.run(db, "INSERT INTO nvidia_attempts(user_id,created_at,status) VALUES(?,?,?)",
                            ("another-user", time.time() - 120, "success"))
        self.assertEqual(self.send(sid).get_json()["code"],"nvidia_budget")
    def test_cooldown_no_fallback(self):
        sid=self.create().get_json()["session"]["id"]
        with patch.object(backend,"generate_reply",
                          side_effect=backend.GenerationFailure("Wait",429,"ai_rate_limit",90)) as generate:
            self.assertEqual(self.send(sid).headers["Retry-After"],"90")
            self.assertEqual(self.send(sid).get_json()["code"],"nvidia_cooldown")
            self.assertEqual(generate.call_count,1)
    def test_context_bounded(self):
        rows=backend.bounded_history([{"role":"user","content":"x"*5000}]*30)
        self.assertLessEqual(len(rows),12)
        self.assertLessEqual(sum(len(r["content"]) for r in rows),12000)
    def test_failed_generation_no_partial(self):
        sid=self.create().get_json()["session"]["id"]
        with patch.object(backend,"generate_reply",side_effect=backend.GenerationFailure("Fail",502)):
            self.assertEqual(self.send(sid).status_code,502)
        self.assertEqual(self.client.get("/api/sessions/"+sid,headers=self.headers).get_json()["session"]["messages"],[])
        with backend.connect() as db:
            self.assertEqual(db.execute("SELECT status FROM nvidia_attempts").fetchone()["status"], "ai_unavailable")
