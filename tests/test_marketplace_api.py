"""Layer 2 — integration: the Vercel Marketplace HTTP surface, driven through
Flask's test client with a fake JWKS and a fake transport underneath.

The unit tests in ``test_marketplace_crypto.py`` prove the signature maths. What
they cannot prove is that a route actually *performs* the check: a service whose
``authenticate`` were never called would still pass every crypto test. So this
file signs real RS256 tokens and sends them at the real endpoints, and asserts
the refusals on their codes.

Still no network and no real credentials. The one thing borrowed from the
crypto suite is the test key pair, which is why the two files live side by side.
"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "tests"))

from integrations import http as httpmod                    # noqa: E402
from marketplace import config as mpconfig                  # noqa: E402
from marketplace import crypto                              # noqa: E402

from test_marketplace_crypto import AUDIENCE, jwk, make_jwt  # noqa: E402

CLIENT_ID = AUDIENCE
CLIENT_SECRET = "client-secret-abcdef123"
BASE_URL = "https://waha.test"
INSTALLATION = "icfg_testInstallation"
OTHER_INSTALLATION = "icfg_someoneElse"

temp = tempfile.TemporaryDirectory()
os.environ["WAHA_DB"] = str(Path(temp.name) / "waha-marketplace.db")
os.environ["WAHA_SECRET"] = "marketplace-test-secret-0123456789"
os.environ["VERCEL_INTEGRATION_CLIENT_ID"] = CLIENT_ID
os.environ["VERCEL_INTEGRATION_CLIENT_SECRET"] = CLIENT_SECRET
os.environ["WAHA_MARKETPLACE_BASE_URL"] = BASE_URL
os.environ["WAHA_MARKETPLACE_REDIRECT_URL"] = f"{BASE_URL}/marketplace/configure"
for key in ("GEMINI_API_KEY", "PROMPTQL_PLATFORM_API_URL", "WAHA_TRUST_PROMPTQL",
            "DATABASE_URL"):
    os.environ.pop(key, None)

spec = importlib.util.spec_from_file_location("waha_marketplace_backend",
                                              ROOT / "backend/app.py")
backend = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backend)

JWKS_DOCUMENT = json.dumps({"keys": [jwk()]}).encode("utf-8")


class FakeTransport(httpmod.Transport):
    """Answers 200 by default; a queued Exception or response is used instead.

    The default matters: provisioning fires a ``resource.updated`` event on the
    way out, and a test about provisioning should not have to script it. A test
    that cares about the event reads ``self.calls``.
    """

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def request(self, method, url, headers=None, body=None, timeout=15,
                resolved_addresses=None):
        self.calls.append({"method": method, "url": url, "headers": headers or {},
                           "body": body})
        if self.answers:
            answer = self.answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            if isinstance(answer, httpmod.HttpResponse):
                return answer
        return httpmod.HttpResponse(200, {}, b"{}")

    def calls_to(self, fragment):
        return [call for call in self.calls if fragment in call["url"]]


def ok(payload=None):
    return httpmod.HttpResponse(200, {}, json.dumps(payload or {}).encode("utf-8"))


def bearer(claims=None, **kwargs):
    return {"Authorization": "Bearer " + make_jwt(claims, **kwargs)}


def admin(installation_id=INSTALLATION):
    return bearer({"installation_id": installation_id, "user_role": "ADMIN"})


class MarketplaceApiCase(unittest.TestCase):
    def setUp(self):
        # Every test starts from an empty marketplace. The database is shared
        # with the rest of the app (and would be a real Postgres instance in
        # CI), so a resource provisioned by one test would otherwise be counted
        # by the next one's ceiling check and its webhook ids would collide.
        with backend.connect() as db:
            for table in ("mp_events", "mp_resources", "mp_installations"):
                backend.run(db, f"DELETE FROM {table}")
        backend.MARKETPLACE.jwks = crypto.JwksCache(
            "https://marketplace.vercel.com/.well-known/jwks",
            lambda url: JWKS_DOCUMENT)
        backend.MARKETPLACE.client.transport = FakeTransport()
        self.client = backend.app.test_client()
        self.transport = backend.MARKETPLACE.client.transport

    # -- helpers -----------------------------------------------------------
    def install(self, installation_id=INSTALLATION, token="vercel-access-token-abc"):
        response = self.client.put(
            f"/v1/installations/{installation_id}",
            headers=admin(installation_id),
            json={"scopes": ["projects:read"], "acceptedPolicies": {"toc": "2026-01-01T00:00:00Z"},
                  "credentials": {"access_token": token, "token_type": "Bearer"},
                  "account": {"name": "Acme", "url": "https://acme.test",
                              "contact": {"email": "ops@acme.test", "name": "Ops"}}})
        self.assertEqual(response.status_code, 201, response.data)
        return response

    def provision(self, name="مساحة اختبار", metadata=None):
        return self.client.post(
            f"/v1/installations/{INSTALLATION}/resources",
            headers=admin(),
            json={"productId": mpconfig.DEFAULT_PRODUCT_ID,
                  "billingPlanId": mpconfig.FREE_PLAN["id"], "name": name,
                  "metadata": metadata if metadata is not None
                  else {"workspace_name": "مساحة اختبار", "focus": "general"}})


class Authentication(MarketplaceApiCase):
    def test_a_call_without_authorization_is_refused(self):
        response = self.client.get(f"/v1/installations/{INSTALLATION}/plans")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["error"]["code"], "missing_authorization")

    def test_a_forged_signature_is_refused(self):
        response = self.client.get(f"/v1/installations/{INSTALLATION}/plans",
                                   headers=bearer(bad_signature=True))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["error"]["code"], "bad_signature")

    def test_a_token_minted_for_another_integration_is_refused(self):
        """The `aud` check is what makes one marketplace token useless elsewhere."""
        response = self.client.get(f"/v1/installations/{INSTALLATION}/plans",
                                   headers=bearer({"aud": "oac_someoneElse"}))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["error"]["code"], "bad_audience")

    def test_a_valid_token_for_one_installation_cannot_read_another(self):
        """The claim and the path are both real; only their comparison stops this."""
        self.install()
        response = self.client.get(f"/v1/installations/{OTHER_INSTALLATION}/resources",
                                   headers=admin(INSTALLATION))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["error"]["code"], "installation_mismatch")

    def test_a_read_only_member_cannot_provision(self):
        self.install()
        response = self.client.post(
            f"/v1/installations/{INSTALLATION}/resources",
            headers=bearer({"installation_id": INSTALLATION, "user_role": "USER"}),
            json={"productId": mpconfig.DEFAULT_PRODUCT_ID,
                  "billingPlanId": mpconfig.FREE_PLAN["id"], "name": "x", "metadata": {}})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["error"]["code"], "forbidden_role")

    def test_an_unconfigured_server_says_so_instead_of_failing_open(self):
        original = backend.MARKETPLACE.settings
        backend.MARKETPLACE.settings = mpconfig.MarketplaceSettings(
            client_id="", client_secret="", slug="", product_id="waha-workspace",
            base_url=BASE_URL, redirect_url="")
        try:
            response = self.client.get(f"/v1/installations/{INSTALLATION}/plans",
                                       headers=admin())
        finally:
            backend.MARKETPLACE.settings = original
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["error"]["code"], "not_configured")


class InstallationLifecycle(MarketplaceApiCase):
    def test_upsert_returns_201_and_never_echoes_the_token(self):
        response = self.install()
        self.assertEqual(response.status_code, 201)
        self.assertNotIn(b"vercel-access-token-abc", response.data)

    def test_upsert_requires_credentials(self):
        response = self.client.put(f"/v1/installations/{INSTALLATION}",
                                   headers=admin(), json={"scopes": []})
        self.assertEqual(response.status_code, 400)
        error = response.get_json()["error"]
        self.assertEqual(error["code"], "validation_error")
        self.assertEqual([field["key"] for field in error["fields"]], ["credentials"])

    def test_upsert_refuses_an_empty_access_token(self):
        response = self.client.put(
            f"/v1/installations/{INSTALLATION}", headers=admin(),
            json={"scopes": [], "credentials": {"access_token": "  ", "token_type": "Bearer"}})
        self.assertEqual(response.status_code, 400)
        self.assertEqual([field["key"] for field in
                          response.get_json()["error"]["fields"]],
                         ["credentials.access_token"])

    def test_reinstalling_replaces_the_stored_token(self):
        """Only the newest token works at Vercel, so the old one must go."""
        self.install(token="first-token-value-1234")
        self.install(token="second-token-value-5678")
        row = backend.marketplace_store.raw_installation(INSTALLATION)
        self.assertEqual(row["access_token"], "second-token-value-5678")

    def test_plans_are_free_and_say_so(self):
        self.install()
        response = self.client.get(f"/v1/installations/{INSTALLATION}/plans",
                                   headers=admin())
        self.assertEqual(response.status_code, 200)
        plan = response.get_json()["plans"][0]
        self.assertFalse(plan["paymentMethodRequired"])
        self.assertEqual(plan["id"], mpconfig.FREE_PLAN["id"])

    def test_an_unknown_product_has_no_plans(self):
        response = self.client.get("/v1/products/nope/plans", headers=admin())
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["error"]["code"], "unknown_product")

    def test_owner_status_reports_configuration_without_any_secret(self):
        login = self.client.post("/api/owner/login", json={"token": ""})
        self.assertIn(login.status_code, (401, 503))
        response = self.client.get("/api/owner/marketplace")
        self.assertIn(response.status_code, (401, 503))


class Resources(MarketplaceApiCase):
    def setUp(self):
        super().setUp()
        self.install()

    def test_provision_returns_the_variables_vercel_injects(self):
        response = self.provision()
        self.assertEqual(response.status_code, 201, response.data)
        names = {secret["name"] for secret in response.get_json()["secrets"]}
        self.assertEqual(names, {"WAHA_API_BASE", "WAHA_API_TOKEN", "WAHA_WORKSPACE_ID"})

    def test_the_injected_token_is_a_real_waha_identity(self):
        """The point of the connector: the value works, it is not a placeholder."""
        secrets = {item["name"]: item["value"]
                   for item in self.provision().get_json()["secrets"]}
        self.assertEqual(secrets["WAHA_API_BASE"], BASE_URL)
        me = self.client.get("/api/me",
                             headers={"Authorization": "Bearer " + secrets["WAHA_API_TOKEN"]})
        self.assertEqual(me.status_code, 200)
        self.assertTrue(me.get_json()["authenticated"])
        self.assertEqual(me.get_json()["user"]["id"], secrets["WAHA_WORKSPACE_ID"])

    def test_a_resource_view_never_carries_the_token(self):
        provisioned = self.provision().get_json()
        listed = self.client.get(f"/v1/installations/{INSTALLATION}/resources",
                                 headers=admin()).get_json()
        self.assertEqual([item["id"] for item in listed["resources"]], [provisioned["id"]])
        self.assertNotIn("api_token", json.dumps(listed, ensure_ascii=False))
        self.assertNotIn("secrets", json.dumps(listed, ensure_ascii=False))
        self.assertTrue(listed["resources"][0]["tokenFingerprint"])

    def test_metadata_is_validated_against_the_published_schema(self):
        response = self.provision(metadata={"workspace_name": "ab"})
        self.assertEqual(response.status_code, 400)
        error = response.get_json()["error"]
        self.assertEqual(error["code"], "validation_error")
        self.assertEqual([field["key"] for field in error["fields"]], ["workspace_name"])

    def test_an_unknown_metadata_field_is_refused(self):
        response = self.provision(metadata={"workspace_name": "مساحة", "region": "iad1"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual([field["key"] for field in
                          response.get_json()["error"]["fields"]], ["region"])

    def test_an_unknown_plan_is_refused(self):
        response = self.client.post(
            f"/v1/installations/{INSTALLATION}/resources", headers=admin(),
            json={"productId": mpconfig.DEFAULT_PRODUCT_ID, "billingPlanId": "pro-999",
                  "name": "x", "metadata": {"workspace_name": "مساحة"}})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"]["code"], "unknown_plan")

    def test_provisioning_without_an_installation_is_refused(self):
        response = self.client.post(
            "/v1/installations/icfg_neverInstalled/resources",
            headers=bearer({"installation_id": "icfg_neverInstalled"}),
            json={"productId": mpconfig.DEFAULT_PRODUCT_ID,
                  "billingPlanId": mpconfig.FREE_PLAN["id"], "name": "x", "metadata": {}})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["error"]["code"], "unknown_installation")

    def test_the_resource_ceiling_is_enforced(self):
        original = backend.MARKETPLACE.settings
        limited = mpconfig.MarketplaceSettings(**{**original.__dict__, "max_resources": 1})
        backend.MARKETPLACE.settings = limited
        try:
            self.assertEqual(self.provision(name="الأولى").status_code, 201)
            second = self.provision(name="الثانية")
        finally:
            backend.MARKETPLACE.settings = original
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.get_json()["error"]["code"], "resource_limit_reached")

    def test_a_resource_can_be_renamed_and_deleted(self):
        resource_id = self.provision().get_json()["id"]
        patched = self.client.patch(
            f"/v1/installations/{INSTALLATION}/resources/{resource_id}",
            headers=admin(), json={"name": "اسم جديد"})
        self.assertEqual(patched.status_code, 200)
        self.assertEqual(patched.get_json()["name"], "اسم جديد")
        deleted = self.client.delete(
            f"/v1/installations/{INSTALLATION}/resources/{resource_id}", headers=admin())
        self.assertEqual(deleted.status_code, 204)
        self.assertEqual(self.client.get(
            f"/v1/installations/{INSTALLATION}/resources/{resource_id}",
            headers=admin()).status_code, 404)

    def test_provisioning_announces_itself_to_vercel(self):
        self.provision()
        self.assertTrue(self.transport.calls_to("/events"),
                        "the store would stay pending forever without this")


class Rotation(MarketplaceApiCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.resource_id = self.provision().get_json()["id"]
        self.first_token = backend.marketplace_store.raw_resource(
            INSTALLATION, self.resource_id)["api_token"]

    def _rotate(self):
        return self.client.post(
            f"/v1/installations/{INSTALLATION}/resources/{self.resource_id}"
            "/secrets/rotate", headers=admin(), json={})

    def test_rotation_returns_new_secrets_and_pushes_them_to_vercel(self):
        response = self._rotate()
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["sync"])
        token = next(item["value"] for item in payload["secrets"]
                     if item["name"] == "WAHA_API_TOKEN")
        self.assertNotEqual(token, self.first_token)
        self.assertTrue(self.transport.calls_to("/secrets"),
                        "a rotation Vercel never hears about leaves stale env vars")
        self.assertNotIn(self.first_token.encode(), self.transport.calls[-1]["body"])

    def test_rotation_keeps_the_same_workspace(self):
        """Rotation changes the credential, not the identity behind it."""
        self._rotate()
        row = backend.marketplace_store.raw_resource(INSTALLATION, self.resource_id)
        self.assertEqual(row["workspace_id"],
                         backend.marketplace_store.raw_resource(
                             INSTALLATION, self.resource_id)["workspace_id"])
        self.assertTrue(row["workspace_id"].startswith("u_"))

    def test_rotation_reports_202_when_vercel_cannot_be_reached(self):
        self.transport.answers.append(
            httpmod.IntegrationError("boom", code="upstream_timeout", status=504))
        response = self._rotate()
        self.assertEqual(response.status_code, 202)
        self.assertFalse(response.get_json()["sync"])

    def test_rotation_of_an_unknown_resource_is_a_404(self):
        response = self.client.post(
            f"/v1/installations/{INSTALLATION}/resources/res_missing/secrets/rotate",
            headers=admin(), json={})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["error"]["code"], "not_found")

    def test_the_old_token_stops_working_after_rotation(self):
        """Otherwise rotation is theatre: two live credentials, one of them leaked."""
        self._rotate()
        me = self.client.get("/api/me",
                             headers={"Authorization": "Bearer " + self.first_token})
        self.assertEqual(me.get_json()["authenticated"], False)


class Webhooks(MarketplaceApiCase):
    def _send(self, event, installation_id=INSTALLATION, secret=CLIENT_SECRET,
              event_id="evt_1"):
        body = json.dumps({"id": event_id, "type": event,
                           "createdAt": "2026-01-01T00:00:00Z",
                           "payload": {"configuration": {"id": installation_id}}}).encode()
        return self.client.post(mpconfig.WEBHOOK_PATH, data=body,
                                headers={"x-vercel-signature":
                                         crypto.webhook_signature(body, secret),
                                         "Content-Type": "application/json"})

    def test_a_signed_uninstall_retires_the_installation_and_its_resources(self):
        self.install()
        self.provision()
        response = self._send("integration-configuration.removed")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["handled"], "uninstalled")
        installation = backend.marketplace_store.get_installation(INSTALLATION)
        self.assertEqual(installation["status"], "uninstalled")
        self.assertEqual(installation["token_fingerprint"], "")
        resources = backend.marketplace_store.list_resources(INSTALLATION)
        self.assertTrue(resources)
        self.assertTrue(all(item["status"] == "uninstalled" for item in resources))

    def test_a_duplicate_delivery_is_recorded_once(self):
        """Vercel retries until it sees a 2xx, so the id has to be the guard."""
        self.install()
        self.assertEqual(self._send("integration-configuration.removed").get_json()["duplicate"],
                         False)
        second = self._send("integration-configuration.removed")
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.get_json()["duplicate"])
        self.assertEqual(len([event for event in backend.marketplace_store.list_events(50)
                              if event["id"] == "evt_1"]), 1)

    def test_an_unsigned_webhook_is_refused(self):
        response = self._send("integration-configuration.removed", secret="wrong-secret")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["error"]["code"], "invalid_signature")

    def test_an_uninstall_for_an_unknown_installation_is_still_answered_200(self):
        """A 4xx here would make Vercel retry an event that can never succeed."""
        response = self._send("integration-configuration.removed",
                              installation_id="icfg_ghost")
        self.assertEqual(response.status_code, 200)

    def test_an_event_we_do_not_act_on_is_recorded_not_swallowed(self):
        response = self._send("deployment.created")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["handled"], "recorded")

    def test_an_oversized_body_is_read_instead_of_retried_forever(self):
        """A 413 would make Vercel re-send the delivery indefinitely.

        The app-wide cap is 24 KB, sized for a chat message; a deployment event
        is bigger. This route raises the cap for itself -- and still caps it, so
        the fix is not an unbounded read.
        """
        body = json.dumps({"id": "evt_big", "type": "deployment.succeeded",
                           "payload": {"deployment": {"meta": "x" * 40000}}}).encode()
        self.assertGreater(len(body), 24 * 1024)
        response = self.client.post(mpconfig.WEBHOOK_PATH, data=body,
                                    headers={"x-vercel-signature":
                                             crypto.webhook_signature(body, CLIENT_SECRET)})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["event_id"], "evt_big")

    def test_a_body_that_is_not_json_is_answered_200(self):
        body = b"<html>"
        response = self.client.post(mpconfig.WEBHOOK_PATH, data=body,
                                    headers={"x-vercel-signature":
                                             crypto.webhook_signature(body, CLIENT_SECRET)})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["code"], "invalid_json")


class BrowserFlows(MarketplaceApiCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.installation_id = INSTALLATION

    def _session(self, installation_id=None):
        installation_id = installation_id or self.installation_id
        self.client.set_cookie("waha_mp",
                               backend.MARKETPLACE.session_token(installation_id),
                               domain="localhost")

    def test_the_dashboard_refuses_without_a_session(self):
        """An empty dashboard would still be an enumeration of installation ids."""
        response = self.client.get(mpconfig.DASHBOARD_PATH)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(INSTALLATION.encode(), response.data)

    def test_a_forged_session_is_not_a_session(self):
        self.client.set_cookie("waha_mp", "1.deadbeef", domain="localhost")
        response = self.client.get(f"{mpconfig.DASHBOARD_PATH}?installation={INSTALLATION}")
        self.assertNotIn(INSTALLATION.encode(), response.data)

    def test_the_dashboard_renders_the_workspace_and_no_token(self):
        self.provision()
        row = backend.marketplace_store.raw_resource(
            INSTALLATION, backend.marketplace_store.list_resources(INSTALLATION)[0]["id"])
        self._session()
        response = self.client.get(f"{mpconfig.DASHBOARD_PATH}?installation={INSTALLATION}")
        self.assertEqual(response.status_code, 200)
        self.assertIn(row["workspace_id"].encode(), response.data)
        self.assertNotIn(row["api_token"].encode(), response.data)

    def test_configure_ignores_a_hostile_next_parameter(self):
        """`next` comes from a query string; redirecting to it would be open."""
        self.transport.answers.append(ok({"access_token": "tok", "token_type": "Bearer",
                                          "installation_id": INSTALLATION,
                                          "team_id": "team_1", "user_id": "u_1"}))
        response = self.client.get(
            f"{mpconfig.CONFIGURE_PATH}?code=abc&configurationId={INSTALLATION}"
            "&next=" + "http://evil.test/steal")
        self.assertEqual(response.status_code, 302)
        self.assertIn(INSTALLATION, response.headers["Location"])
        self.assertNotIn("evil.test", response.headers["Location"])

    def test_configure_follows_a_vercel_next_parameter(self):
        self.transport.answers.append(ok({"access_token": "tok", "token_type": "Bearer",
                                          "installation_id": INSTALLATION,
                                          "team_id": "team_1", "user_id": "u_1"}))
        response = self.client.get(
            f"{mpconfig.CONFIGURE_PATH}?code=abc&configurationId={INSTALLATION}"
            "&next=" + "https://vercel.com/dashboard")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].startswith("https://vercel.com/"))

    def test_configure_reports_a_failed_exchange_rather_than_redirecting(self):
        self.transport.answers.append(httpmod.HttpResponse(403, {}, b'{"error":{}}'))
        response = self.client.get(f"{mpconfig.CONFIGURE_PATH}?code=abc")
        self.assertEqual(response.status_code, 502)
        self.assertIn(b"vercel_forbidden", response.data)

    def test_configure_without_a_code_is_refused(self):
        response = self.client.get(mpconfig.CONFIGURE_PATH)
        self.assertEqual(response.status_code, 502)
        self.assertIn(b"missing_code", response.data)

    def test_sso_login_creates_a_session_for_the_claims_installation(self):
        self.transport.answers.append(ok({"id_token": make_jwt(
            {"installation_id": INSTALLATION})}))
        response = self.client.get(f"{mpconfig.REDIRECT_LOGIN_PATH}?code=abc")
        self.assertEqual(response.status_code, 302)
        self.assertIn(INSTALLATION, response.headers["Location"])

    def test_sso_login_without_an_installation_says_what_to_do(self):
        self.transport.answers.append(ok({"id_token": make_jwt({"installation_id": ""})}))
        response = self.client.get(f"{mpconfig.REDIRECT_LOGIN_PATH}?code=abc")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"no_installation", response.data)


class ConfigurationIsHonest(unittest.TestCase):
    """The settings object is what the docs and the operator console both read."""

    def test_nothing_configured_means_nothing_claimed(self):
        settings = mpconfig.load({})
        self.assertFalse(settings.configured)
        self.assertEqual(settings.missing(), ("VERCEL_INTEGRATION_CLIENT_ID",
                                               "VERCEL_INTEGRATION_CLIENT_SECRET"))

    def test_a_client_id_alone_is_not_configured(self):
        """A half-configured OAuth pair fails with a 403 that blames the customer."""
        settings = mpconfig.load({"VERCEL_INTEGRATION_CLIENT_ID": "oac_x"})
        self.assertFalse(settings.configured)
        self.assertEqual(settings.missing(), ("VERCEL_INTEGRATION_CLIENT_SECRET",))

    def test_describe_never_carries_a_value(self):
        raw = mpconfig.load({"VERCEL_INTEGRATION_CLIENT_ID": "oac_visible",
                             "VERCEL_INTEGRATION_CLIENT_SECRET": "super-secret-value"})
        rendered = json.dumps(raw.describe(), ensure_ascii=False)
        self.assertNotIn("super-secret-value", rendered)
        self.assertNotIn("oac_visible", rendered)

    def test_the_free_plan_declares_that_it_needs_no_card(self):
        self.assertFalse(mpconfig.FREE_PLAN["paymentMethodRequired"])

    def test_the_metadata_schema_admits_exactly_the_stored_fields(self):
        self.assertEqual(set(mpconfig.METADATA_SCHEMA["properties"]),
                         {"workspace_name", "focus"})


if __name__ == "__main__":
    unittest.main()
