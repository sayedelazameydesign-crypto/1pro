"""Layer 1 + 2 for the four-provider portal: Render, Google Drive, and the gate.

``test_integrations_core.py`` and ``test_integrations_api.py`` were written when the
surface had two providers. This file is the same two layers for the two that were
added, because the rules a new client must not break are exactly the rules those
files exist to hold: no secret in any message, no invented status, no network in a
test, and no route that reaches a vendor before the owner gate has been satisfied.

Run: python -m unittest tests.test_integrations_portal
"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from integrations import config as cfgmod            # noqa: E402
from integrations import http as httpmod             # noqa: E402
from integrations import redact as redactmod          # noqa: E402
from integrations.drive import DriveClient           # noqa: E402
from integrations.render import RenderClient          # noqa: E402
from integrations.service import IntegrationService   # noqa: E402

OWNER_TOKEN = "owner-secret-token-abcdef"
GITHUB_TOKEN = "ghp_thisIsAFakeToken1234"
VERCEL_TOKEN = "vercel_thisIsAFakeToken"
HOOK_URL = "https://api.vercel.com/v1/integrations/deploy/hooksecretvalue/abc"
RENDER_KEY = "rnd_key_faked_faked_faked"
RENDER_HOOK = "https://api.render.com/deploy/srv-9zxc?key=supersecretkeyvalue"
DRIVE_REFRESH = "1//0fakeRefreshTokenValueForTestsOnly"
DRIVE_ACCESS = "ya29.a0fakeAccessTokenValueForTestsOnly"
DRIVE_CLIENT_SECRET = "GOCSPX-fakeClientSecret"
DRIVE_FOLDER = "1FakeFolderIdForTestsOnlyAAAAAAAAAAA"

# Every variable the portal knows about. A provider missing from this dict is what
# ``load({})`` reports as unconfigured, and the tests below depend on that.
FULL_ENV = {
    "WAHA_OWNER_TOKEN": OWNER_TOKEN,
    "GITHUB_TOKEN": GITHUB_TOKEN,
    "GITHUB_REPO": "acme/widgets",
    "GITHUB_WORKFLOW_ID": "ci.yml",
    "VERCEL_TOKEN": VERCEL_TOKEN,
    "VERCEL_PROJECT_ID": "prj_123",
    "DEPLOY_HOOK_URL": HOOK_URL,
    "RENDER_API_KEY": RENDER_KEY,
    "RENDER_SERVICE_ID": "srv-9zxc",
    "RENDER_DEPLOY_HOOK_URL": RENDER_HOOK,
    "GOOGLE_DRIVE_REFRESH_TOKEN": DRIVE_REFRESH,
    "GOOGLE_DRIVE_CLIENT_ID": "client.apps.googleusercontent.com",
    "GOOGLE_DRIVE_CLIENT_SECRET": DRIVE_CLIENT_SECRET,
    "GOOGLE_DRIVE_FOLDER_ID": DRIVE_FOLDER,
}

PUBLIC_IP = "93.184.216.34"


def public_resolver(host):
    return [PUBLIC_IP]


def response(status=200, payload=None, headers=None, raw=None):
    body = raw if raw is not None else json.dumps(payload or {}).encode("utf-8")
    return httpmod.HttpResponse(status, headers or {}, body)


class FakeTransport(httpmod.Transport):
    """Records every call, answers from a queue. Nothing leaves the process."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def request(self, method, url, headers=None, body=None, timeout=15,
                resolved_addresses=None):
        self.calls.append({"method": method, "url": url, "headers": headers or {},
                           "body": body, "timeout": timeout,
                           "resolved_addresses": resolved_addresses})
        if not self.answers:
            raise AssertionError("fake transport called more times than scripted")
        return self.answers.pop(0)


class PortalConfigTests(unittest.TestCase):
    def test_an_empty_environment_reports_both_new_providers_missing(self):
        cfg = cfgmod.load({})
        self.assertFalse(cfg.render.configured)
        self.assertFalse(cfg.render.hook_configured)
        self.assertFalse(cfg.drive.configured)
        self.assertEqual(cfg.render.missing(),
                         ("RENDER_API_KEY", "RENDER_SERVICE_ID",
                          "RENDER_DEPLOY_HOOK_URL"))
        self.assertTrue(any("GOOGLE_DRIVE_ACCESS_TOKEN" in name
                            for name in cfg.drive.missing()), cfg.drive.missing())

    def test_a_partial_drive_triple_is_not_configured(self):
        # A refresh token with no client secret cannot mint anything. Reporting it
        # as configured would turn a paste mistake into a 401 from Google, which
        # the surface is documented to read as a revoked grant.
        for drop in ("GOOGLE_DRIVE_REFRESH_TOKEN", "GOOGLE_DRIVE_CLIENT_ID",
                     "GOOGLE_DRIVE_CLIENT_SECRET"):
            env = {k: v for k, v in FULL_ENV.items() if k != drop}
            self.assertFalse(cfgmod.load(env).drive.configured, drop)

    def test_a_bare_access_token_is_enough(self):
        cfg = cfgmod.load({"GOOGLE_DRIVE_ACCESS_TOKEN": DRIVE_ACCESS})
        self.assertTrue(cfg.drive.configured)
        self.assertEqual(cfg.drive.missing(), ())
        self.assertFalse(cfg.drive.folder_configured)

    def test_describe_names_the_credential_shape_but_never_its_value(self):
        dumped = json.dumps(cfgmod.load(FULL_ENV).describe())
        for secret in (RENDER_KEY, RENDER_HOOK, DRIVE_REFRESH, DRIVE_CLIENT_SECRET):
            self.assertNotIn(secret, dumped)
        render = cfgmod.load(FULL_ENV).describe()["render"]
        self.assertEqual(render["service_id"], "srv-9zxc")
        self.assertTrue(render["has_deploy_hook"])
        # The trial path and the durable path are different facts about the same
        # capability, so describe() separates them.
        self.assertEqual(cfgmod.load(FULL_ENV).describe()["drive"]["credential"],
                         "refresh_token")
        self.assertEqual(
            cfgmod.load({"GOOGLE_DRIVE_ACCESS_TOKEN": DRIVE_ACCESS})
            .describe()["drive"]["credential"], "access_token")

    def test_the_secrets_list_covers_every_new_credential(self):
        secrets = cfgmod.load(FULL_ENV).secrets()
        # Render's hook carries its secret in the query string, so the whole URL
        # has to be a secret, not just a key inside it.
        self.assertIn(RENDER_HOOK, secrets)
        self.assertIn(RENDER_KEY, secrets)
        self.assertIn(DRIVE_REFRESH, secrets)
        self.assertIn(DRIVE_CLIENT_SECRET, secrets)

    def test_a_config_without_the_new_providers_still_builds(self):
        # The portal grew two sections; a caller that constructs the config by
        # hand (tests, scripts) must not start failing on a missing attribute.
        cfg = cfgmod.IntegrationConfig(
            owner=cfgmod.OwnerSettings(token=OWNER_TOKEN),
            github=cfgmod.GitHubSettings(token=GITHUB_TOKEN, repo="a/b",
                                       workflow=""),
            vercel=cfgmod.VercelSettings(token=VERCEL_TOKEN, project_id="prj_1",
                                         team_id="", deploy_hook=HOOK_URL))
        self.assertFalse(cfg.render.configured)
        self.assertFalse(cfg.drive.configured)
        self.assertEqual(cfg.render.missing(),
                         ("RENDER_API_KEY", "RENDER_SERVICE_ID",
                          "RENDER_DEPLOY_HOOK_URL"))


class RenderClientTests(unittest.TestCase):
    def setUp(self):
        cfg = cfgmod.load(FULL_ENV)
        self.settings = cfg.render
        self.redact = redactmod.build_redactor(cfg.secrets())

    def client(self, *answers):
        transport = FakeTransport(*answers)
        return (RenderClient(self.settings, self.redact, transport=transport,
                             resolver=public_resolver), transport)

    def test_the_deploy_log_is_unwrapped_and_narrowed(self):
        client, transport = self.client(response(200, [{
            "deploy": {"id": "dep-1", "status": "live", "createdAt": "2026-10-08T10:00:00Z",
                       "commit": {"id": "a" * 40, "ref": "main", "message": "fix: x\n\nbody",
                                  "url": "https://github.com/acme/widgets/commit/1",
                                  "author": {"email": "owner@example.com"}}}}]))
        out = client.list_deploys(limit=5)
        self.assertEqual(out["count"], 1)
        row = out["items"][0]
        self.assertEqual(row["sha"], "a" * 12)
        self.assertEqual(row["title"], "fix: x")
        self.assertEqual(row["state"], "live")
        self.assertIn("/services/srv-9zxc/deploys?limit=5", transport.calls[0]["url"])
        dumped = json.dumps(out)
        self.assertNotIn("owner@example.com", dumped, "the commit author travelled")
        self.assertNotIn("body", dumped, "the whole commit message travelled")

    def test_an_object_shaped_answer_is_a_broken_contract_not_an_empty_log(self):
        # "no deploys" and "I could not read the answer" are different claims, and
        # only the first one is safe to paint as an empty table.
        client, _ = self.client(response(200, {"message": "unexpected"}))
        with self.assertRaises(httpmod.IntegrationError) as caught:
            client.list_deploys()
        self.assertEqual(caught.exception.code, "bad_upstream_payload")

    def test_a_rejected_key_is_named_as_a_credential_problem(self):
        client, _ = self.client(response(401, {"message": "Bad credentials"}))
        with self.assertRaises(httpmod.IntegrationError) as caught:
            client.list_deploys()
        self.assertEqual(caught.exception.code, "render_unauthorized")

    def test_an_unconfigured_provider_is_refused_before_any_path_is_built(self):
        client = RenderClient(cfgmod.RenderSettings(api_key="", service_id="",
                                                    deploy_hook=""),
                              redactmod.identity_redactor(),
                              transport=FakeTransport())
        with self.assertRaises(httpmod.IntegrationError) as caught:
            client.list_deploys()
        self.assertEqual((caught.exception.code, caught.exception.status),
                         ("not_configured", 503))

    def test_the_hook_is_pinned_to_render_and_never_echoes_the_url(self):
        client, transport = self.client(response(200, raw=b"Deploy queued"))
        out = client.trigger_deploy_hook()
        self.assertTrue(out["triggered"])
        self.assertEqual(transport.calls[0]["method"], "POST")
        self.assertEqual(transport.calls[0]["resolved_addresses"], (PUBLIC_IP,))
        self.assertNotIn("supersecretkeyvalue", json.dumps(out))
        self.assertNotIn(RENDER_HOOK, json.dumps(out))

    def test_a_hook_failure_reports_the_text_with_the_key_scrubbed(self):
        client, _ = self.client(response(403,
                                         raw=('"rejected for ' + RENDER_HOOK + '"').encode()))
        with self.assertRaises(httpmod.IntegrationError) as caught:
            client.trigger_deploy_hook()
        self.assertNotIn("supersecretkeyvalue", caught.exception.message)
        self.assertIn(redactmod.REDACTED, caught.exception.message)

    def test_a_hook_url_on_another_host_is_refused(self):
        settings = cfgmod.RenderSettings(api_key=RENDER_KEY, service_id="srv-9zxc",
                                         deploy_hook="https://example.com/deploy?key=k")
        client = RenderClient(settings, redactmod.identity_redactor(),
                              transport=FakeTransport(), resolver=public_resolver)
        with self.assertRaises(httpmod.IntegrationError) as caught:
            client.trigger_deploy_hook()
        self.assertEqual(caught.exception.code, "host_not_allowed")


class DriveClientTests(unittest.TestCase):
    def setUp(self):
        cfg = cfgmod.load(FULL_ENV)
        self.settings = cfg.drive
        self.redact = redactmod.build_redactor(cfg.secrets())

    def client(self, *answers, clock=None):
        transport = FakeTransport(*answers)
        kwargs = {"clock": clock} if clock is not None else {}
        return (DriveClient(self.settings, self.redact, transport=transport, **kwargs),
                transport)

    def test_the_refresh_token_is_exchanged_before_the_call(self):
        client, transport = self.client(response(200, {"access_token": "ya29.minted",
                                                        "expires_in": 3600}),
                                        response(200, {"files": []}))
        out = client.list_files(limit=3)
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(transport.calls[0]["url"], cfgmod.GOOGLE_TOKEN_URI)
        self.assertEqual(transport.calls[0]["method"], "POST")
        self.assertIn(b"grant_type=refresh_token", transport.calls[0]["body"])
        self.assertEqual(transport.calls[1]["headers"]["Authorization"],
                         "Bearer ya29.minted")
        self.assertEqual(out["count"], 0)

    def test_the_minted_token_is_reused_until_it_nearly_expires(self):
        now = [1_700_000_000.0]

        def clock():
            return now[0]

        client, transport = self.client(
            response(200, {"access_token": "ya29.minted", "expires_in": 3600}),
            response(200, {"files": []}),
            response(200, {"files": []}),
            response(200, {"access_token": "ya29.second", "expires_in": 3600}),
            response(200, {"files": []}),
            clock=clock)
        client.list_files()
        client.list_files()                       # cached
        self.assertEqual(len(transport.calls), 3)
        now[0] += 3600                            # past the safety margin
        client.list_files()
        self.assertEqual(len(transport.calls), 5)
        self.assertEqual(transport.calls[3]["url"], cfgmod.GOOGLE_TOKEN_URI)

    def test_a_pasted_access_token_is_used_verbatim_and_never_refreshed(self):
        settings = cfgmod.DriveSettings(access_token=DRIVE_ACCESS, refresh_token="",
                                        client_id="", client_secret="", folder_id="")
        transport = FakeTransport(response(200, {"files": []}))
        DriveClient(settings, self.redact, transport=transport).list_files()
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(transport.calls[0]["headers"]["Authorization"],
                         "Bearer " + DRIVE_ACCESS)

    def test_the_configured_folder_becomes_a_drive_query(self):
        client, transport = self.client(response(200, {"access_token": "t"}),
                                        response(200, {"files": []}))
        out = client.list_files()
        query = urllib.parse.unquote(transport.calls[1]["url"])
        self.assertIn(f"'{DRIVE_FOLDER}' in parents and trashed = false", query)
        self.assertEqual(out["folder_id"], DRIVE_FOLDER)

    def test_a_quote_in_the_folder_id_is_refused_before_the_query_is_built(self):
        settings = cfgmod.DriveSettings(access_token=DRIVE_ACCESS, refresh_token="",
                                        client_id="", client_secret="",
                                        folder_id="x' or '1'='1")
        client = DriveClient(settings, self.redact, transport=FakeTransport())
        with self.assertRaises(httpmod.IntegrationError) as caught:
            client.list_files()
        self.assertEqual(caught.exception.code, "invalid_config")

    def test_the_listing_keeps_only_the_fields_the_page_paints(self):
        client, _ = self.client(
            response(200, {"access_token": "t"}),
            response(200, {"files": [{"id": "f1", "name": "report.md",
                                      "mimeType": "text/markdown", "size": "2048",
                                      "modifiedTime": "2026-10-08T10:00:00Z",
                                      "webViewLink": "https://drive.google.com/f/f1",
                                      "owners": [{"emailAddress": "owner@example.com",
                                                 "displayName": "Sayed"}],
                                      "permissions": [{"role": "owner"}]}],
                            "nextPageToken": "abc"}))
        out = client.list_files()
        row = out["items"][0]
        self.assertEqual(row["size"], 2048)
        self.assertTrue(out["has_more"])
        dumped = json.dumps(out)
        for leak in ("owner@example.com", "Sayed", "permissions"):
            self.assertNotIn(leak, dumped)

    def test_an_unexpected_answer_is_a_contract_break(self):
        client, _ = self.client(response(200, {"access_token": "t"}),
                                response(200, {"nothing": 1}))
        with self.assertRaises(httpmod.IntegrationError) as caught:
            client.list_files()
        self.assertEqual(caught.exception.code, "bad_upstream_payload")

    def test_a_refused_refresh_is_reported_as_a_credential_problem(self):
        # Google answers this with a 400 whose text names the grant; the code has to
        # say "reconnect", not "the listing is empty".
        client, _ = self.client(response(400, {"error": "invalid_grant",
                                               "error_description": "Bad Request"}))
        with self.assertRaises(httpmod.IntegrationError) as caught:
            client.list_files()
        self.assertEqual(caught.exception.code, "drive_token_failed")

    def test_upload_sends_the_body_as_media_and_returns_only_the_link(self):
        client, transport = self.client(
            response(200, {"access_token": "t"}),
            response(201, {"id": "f9", "name": "note.md", "mimeType": "text/markdown",
                           "webViewLink": "https://drive.google.com/f/f9",
                           "size": "11", "owners": [{"emailAddress": "me@x"}]}))
        out = client.upload_text("note.md", "مرحبا بالعالم")
        self.assertTrue(out["created"])
        call = transport.calls[1]
        self.assertIn("uploadType=media", call["url"])
        self.assertIn("name=note.md", call["url"])
        self.assertIn("parents=" + DRIVE_FOLDER, call["url"])
        self.assertEqual(call["headers"]["Content-Type"], "text/markdown")
        self.assertEqual(call["body"].decode("utf-8"), "مرحبا بالعالم")
        self.assertEqual(out["file"]["id"], "f9")
        self.assertNotIn("me@x", json.dumps(out))

    def test_an_unsafe_name_or_mime_is_handled_before_the_request(self):
        client, transport = self.client()
        for bad in ("", "a/b.md", "x" * 201):
            with self.assertRaises(httpmod.IntegrationError) as caught:
                client.upload_text(bad, "text")
            self.assertEqual(caught.exception.code, "invalid_request", bad)
        self.assertEqual(transport.calls, [], "a rejected name still reached Google")

    def test_a_newline_in_the_mime_type_cannot_become_a_header(self):
        client, transport = self.client(
            response(200, {"access_token": "t"}),
            response(200, {"id": "f9", "name": "n.md"}))
        client.upload_text("n.md", "x", mime_type="text/plain\r\nX-Evil: 1")
        self.assertEqual(transport.calls[1]["headers"]["Content-Type"], "text/plain")

    def test_a_body_over_the_cap_is_refused_rather_than_truncated(self):
        client, transport = self.client()
        with self.assertRaises(httpmod.IntegrationError) as caught:
            client.upload_text("big.md", "x" * (512 * 1024 + 1))
        self.assertEqual(caught.exception.code, "invalid_request")
        self.assertEqual(transport.calls, [])


class PortalServiceTests(unittest.TestCase):
    def service(self, *answers, environ=None):
        cfg = cfgmod.load(FULL_ENV if environ is None else environ)
        transport = FakeTransport(*answers)
        return (IntegrationService(cfg, transport=transport, resolver=public_resolver),
                transport)

    def test_status_reports_four_providers_and_their_capabilities(self):
        service, _ = self.service()
        status = service.status()
        self.assertEqual(set(status) - {"checked_at", "limits", "confirm_phrases"},
                         {"github", "vercel", "render", "drive"})
        self.assertEqual(status["render"]["capabilities"],
                         {"list_deploys": True, "trigger_hook": True})
        self.assertEqual(status["drive"]["capabilities"]["upload"], True)
        self.assertTrue(status["render"]["token_fingerprint"])
        self.assertEqual(status["confirm_phrases"]["render_deploy"], "deploy-render")

    def test_status_never_carries_a_value_it_could_not_have_shown(self):
        service, _ = self.service()
        dumped = json.dumps(service.status())
        for secret in (OWNER_TOKEN, GITHUB_TOKEN, VERCEL_TOKEN, HOOK_URL, RENDER_KEY,
                       RENDER_HOOK, DRIVE_REFRESH, DRIVE_CLIENT_SECRET, DRIVE_ACCESS):
            self.assertNotIn(secret, dumped)

    def test_an_unconfigured_provider_reports_missing_instead_of_zero(self):
        env = {k: v for k, v in FULL_ENV.items()
               if not k.startswith(("RENDER_", "GOOGLE_DRIVE_"))}
        service, _ = self.service(environ=env)
        status = service.status()
        self.assertFalse(status["render"]["capabilities"]["list_deploys"])
        self.assertEqual(status["drive"]["capabilities"]["list_files"], False)
        self.assertIn("RENDER_API_KEY", status["render"]["missing"])

    def test_a_read_and_a_write_are_stamped_differently(self):
        # ``fetched_at`` on a read and ``at`` on a write is the existing convention;
        # the new providers follow it so the UI can sort both the same way.
        # Three answers, not four: the access token the listing minted is still
        # valid when the upload runs, so the upload is signed from the cache.
        service, _ = self.service(
            response(200, {"access_token": "t"}), response(200, {"files": []}),
            response(201, {"id": "f9", "name": "note.md"}))
        self.assertIn("fetched_at", service.drive_files())
        self.assertIn("at", service.drive_upload("note.md", "x"))


# --- layer 2: the HTTP surface, with the real gate -----------------------------
temp = tempfile.TemporaryDirectory()
os.environ["WAHA_DB"] = str(Path(temp.name) / "waha-portal.db")
os.environ["WAHA_OWNER_TOKEN"] = OWNER_TOKEN
os.environ["GITHUB_TOKEN"] = GITHUB_TOKEN
os.environ["GITHUB_REPO"] = "acme/widgets"
os.environ["GITHUB_WORKFLOW_ID"] = "ci.yml"
os.environ["VERCEL_TOKEN"] = VERCEL_TOKEN
os.environ["VERCEL_PROJECT_ID"] = "prj_123"
os.environ["DEPLOY_HOOK_URL"] = HOOK_URL
for key in ("RENDER_API_KEY", "RENDER_SERVICE_ID", "RENDER_DEPLOY_HOOK_URL",
            "GOOGLE_DRIVE_ACCESS_TOKEN", "GOOGLE_DRIVE_REFRESH_TOKEN",
            "GOOGLE_DRIVE_CLIENT_ID", "GOOGLE_DRIVE_CLIENT_SECRET",
            "GOOGLE_DRIVE_FOLDER_ID", "GEMINI_API_KEY", "WAHA_TRUST_PROMPTQL"):
    os.environ.pop(key, None)

spec = importlib.util.spec_from_file_location("waha_portal_backend",
                                              ROOT / "backend/app.py")
backend = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backend)


class PortalSurfaceTests(unittest.TestCase):
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

    def login(self):
        payload = self.client.post("/api/owner/login",
                                   json={"token": OWNER_TOKEN}).get_json()
        return {"Authorization": "Bearer " + payload["token"],
                "X-Waha-CSRF": payload["csrf"]}

    def test_the_new_read_routes_refuse_an_anonymous_caller(self):
        for path in ("/api/owner/integrations/render/deploys",
                     "/api/owner/integrations/drive/files"):
            self.assertEqual(self.client.get(path).status_code, 401, path)

    def test_the_new_routes_say_not_configured_rather_than_zero_results(self):
        headers = self.login()
        for path in ("/api/owner/integrations/render/deploys",
                     "/api/owner/integrations/drive/files"):
            result = self.client.get(path, headers=headers)
            self.assertEqual(result.status_code, 503, path)
            self.assertEqual(result.get_json()["code"], "not_configured", path)
            self.assertEqual(self.transport.calls, [], path + " reached the network")

    def test_a_write_without_its_phrase_is_refused_before_the_provider_is_asked(self):
        headers = self.login()
        for path in ("/api/owner/integrations/render/deploy-hook",
                     "/api/owner/integrations/drive/upload"):
            result = self.client.post(path, json={}, headers=headers)
            self.assertEqual(result.status_code, 409, path)
            self.assertEqual(result.get_json()["code"], "confirmation_required", path)

    def test_the_gate_lists_the_two_new_mutations(self):
        self.assertEqual(backend.OWNER_MUTATIONS,
                         {"github_dispatch", "vercel_deploy", "render_deploy",
                          "drive_upload"})
        self.assertEqual(set(cfgmod.CONFIRM_PHRASES), backend.OWNER_MUTATIONS,
                         "a mutation the gate does not know about is a mutation "
                         "that runs unconfirmed")

    def test_a_configured_provider_reads_through_the_same_gate(self):
        configured = cfgmod.load({**FULL_ENV, "GOOGLE_DRIVE_ACCESS_TOKEN": DRIVE_ACCESS,
                                  "GOOGLE_DRIVE_REFRESH_TOKEN": "",
                                  "GOOGLE_DRIVE_CLIENT_ID": "",
                                  "GOOGLE_DRIVE_CLIENT_SECRET": ""})
        backend.INTEGRATIONS = IntegrationService(
            configured, transport=FakeTransport(
                response(200, [{"deploy": {"id": "dep-1", "status": "live",
                                           "commit": {"ref": "main", "id": "b" * 40,
                                                      "message": "x"}}}]),
                response(200, {"files": [{"id": "f1", "name": "a.md",
                                          "mimeType": "text/markdown", "size": "1"}]})),
            resolver=public_resolver)
        headers = self.login()
        deploys = self.client.get("/api/owner/integrations/render/deploys",
                                  headers=headers).get_json()
        files = self.client.get("/api/owner/integrations/drive/files",
                                headers=headers).get_json()
        self.assertEqual(deploys["items"][0]["state"], "live")
        self.assertEqual(files["items"][0]["name"], "a.md")

    def test_the_status_the_login_returns_already_covers_the_four_cards(self):
        # The page paints its cards from the login payload with no second request,
        # so a provider absent here shows as UNKNOWN forever.
        payload = self.client.post("/api/owner/login",
                                   json={"token": OWNER_TOKEN}).get_json()
        status = payload["integrations"]
        for provider in ("github", "vercel", "render", "drive"):
            self.assertIn(provider, status)
            self.assertIn("capabilities", status[provider], provider)


if __name__ == "__main__":
    unittest.main()
