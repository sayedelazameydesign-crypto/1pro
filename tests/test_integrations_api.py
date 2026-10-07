"""Layer 2 — integration: the owner HTTP surface, driven through Flask's test
client with a fake transport underneath.

This is the layer the unit tests cannot reach, because every rule the surface
claims lives at the edge: who may call it, from which origin, with which CSRF
token, how often, and only after typing a confirmation phrase. A unit test on the
client would pass even if `owner_protect` were deleted.

Still no network and no real credentials: `backend.INTEGRATIONS` is replaced by a
service built on a scripted fake transport, so a 401 from GitHub or a 429 from
Vercel can be produced on demand.
"""
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from integrations import config as cfgmod          # noqa: E402
from integrations import http as httpmod           # noqa: E402
from integrations.service import IntegrationService  # noqa: E402

OWNER_TOKEN = "owner-secret-token-abcdef"
GITHUB_TOKEN = "ghp_thisIsAFakeToken1234"
VERCEL_TOKEN = "vercel_thisIsAFakeToken"
HOOK_URL = "https://api.vercel.com/v1/integrations/deploy/hooksecretvalue/abc"

temp = tempfile.TemporaryDirectory()
os.environ["WAHA_DB"] = str(Path(temp.name) / "waha-integrations.db")
os.environ["WAHA_OWNER_TOKEN"] = OWNER_TOKEN
os.environ["WAHA_OWNER_ALLOWED_ORIGINS"] = "https://admin.test"
os.environ["WAHA_ALLOWED_ORIGINS"] = "https://pages.test"
os.environ["GITHUB_TOKEN"] = GITHUB_TOKEN
os.environ["GITHUB_REPO"] = "acme/widgets"
os.environ["GITHUB_WORKFLOW_ID"] = "ci.yml"
os.environ["VERCEL_TOKEN"] = VERCEL_TOKEN
os.environ["VERCEL_PROJECT_ID"] = "prj_123"
os.environ["DEPLOY_HOOK_URL"] = HOOK_URL
for key in ("GEMINI_API_KEY", "PROMPTQL_PLATFORM_API_URL", "WAHA_TRUST_PROMPTQL"):
    os.environ.pop(key, None)

spec = importlib.util.spec_from_file_location("waha_integrations_backend",
                                              ROOT / "backend/app.py")
backend = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backend)


def response(status=200, payload=None, headers=None, raw=None):
    body = raw if raw is not None else json.dumps(payload or {}).encode("utf-8")
    return httpmod.HttpResponse(status, headers or {}, body)


def public_resolver(host):
    """Injected so the deploy-hook guard needs no real DNS. See http.py."""
    return ["93.184.216.34"]


class FakeTransport(httpmod.Transport):
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def request(self, method, url, headers=None, body=None, timeout=15):
        self.calls.append({"method": method, "url": url, "headers": headers or {},
                           "body": body})
        if not self.answers:
            raise AssertionError("fake transport called more times than scripted")
        return self.answers.pop(0)


class OwnerSurfaceTests(unittest.TestCase):
    def setUp(self):
        self.client = backend.app.test_client()
        with backend.connect() as db:
            backend.run(db, "DELETE FROM attempts")
        self.transport = FakeTransport()
        self.original = backend.INTEGRATIONS
        backend.INTEGRATIONS = IntegrationService(backend.INTEGRATIONS_CONFIG,
                                                  transport=self.transport,
                                                  resolver=public_resolver)

    def tearDown(self):
        backend.INTEGRATIONS = self.original

    def script(self, *answers):
        self.transport.answers.extend(answers)

    def login(self):
        result = self.client.post("/api/owner/login", json={"token": OWNER_TOKEN})
        self.assertEqual(result.status_code, 201, result.get_json())
        payload = result.get_json()
        return {"Authorization": "Bearer " + payload["token"],
                "X-Waha-CSRF": payload["csrf"]}

    # -- authentication ----------------------------------------------------
    def test_the_page_itself_is_served(self):
        self.assertEqual(self.client.get("/integrations").status_code, 200)

    def test_every_owner_endpoint_refuses_an_anonymous_caller(self):
        for path in ("/api/owner/session", "/api/owner/integrations",
                     "/api/owner/integrations/github/runs",
                     "/api/owner/integrations/vercel/deployments"):
            self.assertEqual(self.client.get(path).status_code, 401, path)

    def test_a_wrong_owner_token_is_refused(self):
        result = self.client.post("/api/owner/login", json={"token": "not-the-token"})
        self.assertEqual(result.status_code, 401)
        self.assertEqual(result.get_json()["code"], "owner_unauthorized")

    def test_an_empty_owner_token_is_refused(self):
        self.assertEqual(self.client.post("/api/owner/login", json={}).status_code, 401)

    def test_a_visitor_token_cannot_open_the_owner_surface(self):
        # Both tokens are HMAC-signed with the same WAHA_SECRET; only the message
        # prefix separates them. This is the test that proves the prefix matters.
        visitor = backend.issue_token("u_" + "a" * 20)
        result = self.client.get("/api/owner/integrations",
                                 headers={"Authorization": "Bearer " + visitor})
        self.assertEqual(result.status_code, 401)

    def test_a_tampered_signature_is_refused(self):
        token = self.login()["Authorization"].split(" ", 1)[1]
        prefix, issued, _signature = token.split(".")
        forged = f"{prefix}.{issued}." + "0" * 64
        result = self.client.get("/api/owner/integrations",
                                 headers={"Authorization": "Bearer " + forged})
        self.assertEqual(result.status_code, 401)

    def test_login_repeatedly_from_one_ip_hits_its_own_ceiling(self):
        limit = backend.INTEGRATIONS_CONFIG.owner.login_limit_per_hour
        for _ in range(limit):
            self.client.post("/api/owner/login", json={"token": "wrong"})
        result = self.client.post("/api/owner/login", json={"token": OWNER_TOKEN})
        self.assertEqual(result.status_code, 429)
        self.assertEqual(result.get_json()["code"], "owner_login_rate_limit")

    # -- session -----------------------------------------------------------
    def test_a_session_is_eight_hours_long(self):
        payload = self.client.post("/api/owner/login",
                                   json={"token": OWNER_TOKEN}).get_json()
        self.assertEqual(payload["expires_in"], 8 * 3600)
        self.assertTrue(payload["token"].startswith("waha-owner."))

    def test_the_session_reports_its_remaining_life(self):
        headers = self.login()
        payload = self.client.get("/api/owner/session", headers=headers).get_json()
        self.assertTrue(payload["authenticated"])
        self.assertLessEqual(payload["expires_in"], 8 * 3600)
        self.assertGreater(payload["expires_in"], 8 * 3600 - 60)

    def test_an_expired_session_is_refused(self):
        stale = backend.issue_owner_token(now=time.time() - (8 * 3600) - 120)
        result = self.client.get("/api/owner/integrations",
                                 headers={"Authorization": "Bearer " + stale})
        self.assertEqual(result.status_code, 401)

    def test_a_session_from_the_future_is_refused(self):
        future = backend.issue_owner_token(now=time.time() + 600)
        result = self.client.get("/api/owner/integrations",
                                 headers={"Authorization": "Bearer " + future})
        self.assertEqual(result.status_code, 401)

    # -- CSRF --------------------------------------------------------------
    def test_a_mutation_without_csrf_is_refused(self):
        headers = self.login()
        headers.pop("X-Waha-CSRF")
        result = self.client.post("/api/owner/integrations/github/dispatch",
                                  headers=headers, json={"confirm": "dispatch-ci"})
        self.assertEqual(result.status_code, 403)
        self.assertEqual(result.get_json()["code"], "csrf_rejected")

    def test_the_visitor_csrf_value_does_not_work_here(self):
        headers = self.login()
        visitor_csrf = backend.csrf_for("u_" + "a" * 20)
        headers["X-Waha-CSRF"] = visitor_csrf
        result = self.client.post("/api/owner/integrations/github/dispatch",
                                  headers=headers, json={"confirm": "dispatch-ci"})
        self.assertEqual(result.status_code, 403)

    def test_a_non_json_mutation_is_refused_before_the_provider_is_called(self):
        headers = self.login()
        result = self.client.post("/api/owner/integrations/github/dispatch",
                                  headers=headers, data="confirm=dispatch-ci")
        self.assertEqual(result.status_code, 415)
        self.assertEqual(self.transport.calls, [])

    # -- admin CORS --------------------------------------------------------
    def test_a_visitor_origin_cannot_call_the_owner_surface(self):
        # https://pages.test is trusted for chat (WAHA_ALLOWED_ORIGINS) and must
        # still be refused here -- that separation is the whole admin policy.
        headers = self.login()
        result = self.client.get("/api/owner/integrations",
                                 headers={**headers, "Origin": "https://pages.test"})
        self.assertEqual(result.status_code, 403)
        self.assertNotIn("Access-Control-Allow-Origin", result.headers)

    def test_an_owner_origin_is_allowed_and_echoed(self):
        headers = self.login()
        result = self.client.get("/api/owner/integrations",
                                 headers={**headers, "Origin": "https://admin.test"})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.headers["Access-Control-Allow-Origin"], "https://admin.test")

    def test_the_visitor_surface_still_uses_the_visitor_origin_list(self):
        # The admin policy must not have tightened the chat path by accident.
        result = self.client.get("/api/skills", headers={"Origin": "https://pages.test"})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.headers["Access-Control-Allow-Origin"], "https://pages.test")

    # -- confirmation ------------------------------------------------------
    def test_a_mutation_without_a_confirm_phrase_is_refused(self):
        headers = self.login()
        result = self.client.post("/api/owner/integrations/github/dispatch",
                                  headers=headers, json={"ref": "main"})
        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.get_json()["code"], "confirmation_required")
        self.assertEqual(self.transport.calls, [])

    def test_a_wrong_confirm_phrase_is_refused(self):
        headers = self.login()
        result = self.client.post("/api/owner/integrations/github/dispatch",
                                  headers=headers, json={"ref": "main", "confirm": "yes"})
        self.assertEqual(result.status_code, 409)
        self.assertEqual(self.transport.calls, [])

    def test_the_two_mutations_do_not_share_a_phrase(self):
        headers = self.login()
        result = self.client.post("/api/owner/integrations/vercel/deploy-hook",
                                  headers=headers, json={"confirm": "dispatch-ci"})
        self.assertEqual(result.status_code, 409)
        self.assertEqual(self.transport.calls, [])

    # -- write rate limit --------------------------------------------------
    def test_the_fourth_write_in_a_minute_is_refused(self):
        headers = self.login()
        limit = backend.INTEGRATIONS_CONFIG.owner.write_limit_per_minute
        self.assertEqual(limit, 3, "the documented ceiling is 3/min")
        for attempt in range(limit):
            self.script(response(200, {"workflow_runs": []}))
        for _ in range(limit):
            result = self.client.post("/api/owner/integrations/github/dispatch",
                                      headers=headers,
                                      json={"ref": "main", "confirm": "dispatch-ci"})
            self.assertEqual(result.status_code, 202, result.get_json())
        result = self.client.post("/api/owner/integrations/github/dispatch",
                                  headers=headers,
                                  json={"ref": "main", "confirm": "dispatch-ci"})
        self.assertEqual(result.status_code, 429)
        self.assertEqual(result.get_json()["code"], "owner_write_rate_limit")
        self.assertEqual(len(self.transport.calls), limit,
                         "a refused write must not reach GitHub")

    def test_reads_are_not_spent_from_the_write_budget(self):
        headers = self.login()
        for _ in range(6):
            self.script(response(200, {"workflow_runs": []}))
            self.assertEqual(
                self.client.get("/api/owner/integrations/github/runs",
                                headers=headers).status_code, 200)
        self.script(response(204, raw=b""))
        result = self.client.post("/api/owner/integrations/github/dispatch",
                                  headers=headers,
                                  json={"ref": "main", "confirm": "dispatch-ci"})
        self.assertEqual(result.status_code, 202)

    # -- reads -------------------------------------------------------------
    def test_runs_are_returned_from_the_upstream_payload(self):
        headers = self.login()
        self.script(response(200, {"total_count": 1, "workflow_runs": [
            {"id": 7, "name": "CI", "display_title": "fix", "status": "completed",
             "conclusion": "success", "head_branch": "main", "head_sha": "c" * 40,
             "html_url": "https://github.com/acme/widgets/actions/runs/7"}]}))
        payload = self.client.get("/api/owner/integrations/github/runs",
                                  headers=headers).get_json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["items"][0]["sha"], "c" * 12)
        self.assertIn("fetched_at", payload)

    def test_deployments_are_returned_from_the_upstream_payload(self):
        headers = self.login()
        self.script(response(200, {"deployments": [
            {"uid": "dpl_1", "name": "widgets", "url": "w.vercel.app", "state": "READY",
             "target": "production", "created": 1760000000000}]}))
        payload = self.client.get("/api/owner/integrations/vercel/deployments",
                                  headers=headers).get_json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["items"][0]["state"], "READY")

    # -- mutations ---------------------------------------------------------
    def test_dispatch_reaches_github_with_the_requested_ref(self):
        headers = self.login()
        self.script(response(204, raw=b""))
        result = self.client.post("/api/owner/integrations/github/dispatch",
                                  headers=headers,
                                  json={"ref": "release", "confirm": "dispatch-ci"})
        self.assertEqual(result.status_code, 202)
        self.assertEqual(self.transport.calls[0]["body"], {"ref": "release"})

    def test_the_deploy_hook_is_fired_and_only_its_id_echoed(self):
        headers = self.login()
        self.script(response(200, {"id": "dpl_9", "url": "w.vercel.app",
                                   "state": "QUEUED", "env": {"SECRET": "hunter2"}}))
        payload = self.client.post("/api/owner/integrations/vercel/deploy-hook",
                                   headers=headers,
                                   json={"confirm": "deploy"}).get_json()
        self.assertTrue(payload["triggered"])
        self.assertEqual(payload["deployment_id"], "dpl_9")
        self.assertNotIn("hunter2", json.dumps(payload))

    # -- error mapping and secrets ----------------------------------------
    def test_an_upstream_401_becomes_a_502_not_a_401(self):
        # Forwarding GitHub's 401 would tell the owner *their session* is bad,
        # which is false and sends them to re-login instead of rotating a token.
        headers = self.login()
        self.script(response(401, {"message": "Bad credentials"}))
        result = self.client.get("/api/owner/integrations/github/runs", headers=headers)
        self.assertEqual(result.status_code, 502)
        self.assertEqual(result.get_json()["code"], "ghp_unauthorized")

    def test_an_upstream_429_carries_retry_after_to_the_browser(self):
        headers = self.login()
        self.script(response(429, {"message": "slow"}, headers={"Retry-After": "31"}))
        result = self.client.get("/api/owner/integrations/github/runs", headers=headers)
        self.assertEqual(result.status_code, 504)
        self.assertEqual(result.headers.get("Retry-After"), "31")

    def test_an_unconfigured_provider_is_a_503(self):
        headers = self.login()
        backend.INTEGRATIONS = IntegrationService(cfgmod.load({}), transport=self.transport,
                                                   resolver=public_resolver)
        try:
            result = self.client.get("/api/owner/integrations/github/runs", headers=headers)
            self.assertEqual(result.status_code, 503)
            self.assertEqual(result.get_json()["code"], "not_configured")
        finally:
            backend.INTEGRATIONS = IntegrationService(backend.INTEGRATIONS_CONFIG,
                                                      transport=self.transport,
                                                      resolver=public_resolver)

    def test_no_secret_appears_in_any_owner_response(self):
        headers = self.login()
        self.script(response(200, {"workflow_runs": []}),
                    response(200, {"deployments": []}),
                    response(204, raw=b""),
                    response(200, {"id": "dpl_1"}))
        bodies = [
            self.client.get("/api/owner/integrations", headers=headers).get_data(as_text=True),
            self.client.get("/api/owner/session", headers=headers).get_data(as_text=True),
            self.client.get("/api/owner/integrations/github/runs",
                            headers=headers).get_data(as_text=True),
            self.client.get("/api/owner/integrations/vercel/deployments",
                            headers=headers).get_data(as_text=True),
            self.client.post("/api/owner/integrations/github/dispatch", headers=headers,
                             json={"confirm": "dispatch-ci"}).get_data(as_text=True),
            self.client.post("/api/owner/integrations/vercel/deploy-hook", headers=headers,
                             json={"confirm": "deploy"}).get_data(as_text=True),
        ]
        for secret in (OWNER_TOKEN, GITHUB_TOKEN, VERCEL_TOKEN, HOOK_URL):
            for body in bodies:
                self.assertNotIn(secret, body)

    def test_the_owner_surface_is_listed_in_the_status_payload(self):
        headers = self.login()
        payload = self.client.get("/api/owner/integrations", headers=headers).get_json()
        self.assertEqual(payload["limits"]["write_limit_per_minute"], 3)
        self.assertEqual(payload["limits"]["session_ttl_seconds"], 8 * 3600)
        self.assertEqual(payload["confirm_phrases"]["vercel_deploy"], "deploy")

    def test_the_owner_surface_does_not_leak_into_the_visitor_api(self):
        # A visitor must not be able to read owner state through /api/me or /health.
        for body in (self.client.get("/api/me").get_data(as_text=True),
                     self.client.get("/health").get_data(as_text=True)):
            self.assertNotIn("confirm_phrases", body)
            self.assertNotIn(OWNER_TOKEN, body)


if __name__ == "__main__":
    unittest.main()
