"""Layer 1 — unit: config, redaction, transport guards, both vendor clients.

No network and no real credentials anywhere in this file. Every client is driven
through a fake Transport, which is the only reason the error paths (401, 403, 429,
5xx, malformed body) can be asserted at all: a live test can only ever prove the
happy path, because it cannot make GitHub answer 401 on demand.
"""
import json
import socket
import ssl
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from integrations import config as cfgmod          # noqa: E402
from integrations import http as httpmod           # noqa: E402
from integrations import redact as redactmod       # noqa: E402
from integrations.github import GitHubClient       # noqa: E402
from integrations.service import IntegrationService  # noqa: E402
from integrations.vercel import VercelClient       # noqa: E402

OWNER_TOKEN = "owner-secret-token-abcdef"
GITHUB_TOKEN = "ghp_thisIsAFakeToken1234"
VERCEL_TOKEN = "vercel_thisIsAFakeToken"
HOOK_URL = "https://api.vercel.com/v1/integrations/deploy/hooksecretvalue/abc"

FULL_ENV = {
    "WAHA_OWNER_TOKEN": OWNER_TOKEN,
    "GITHUB_TOKEN": GITHUB_TOKEN,
    "GITHUB_REPO": "acme/widgets",
    "GITHUB_WORKFLOW_ID": "ci.yml",
    "VERCEL_TOKEN": VERCEL_TOKEN,
    "VERCEL_PROJECT_ID": "prj_123",
    "VERCEL_TEAM_ID": "team_9",
    "DEPLOY_HOOK_URL": HOOK_URL,
}


def response(status=200, payload=None, headers=None, raw=None):
    body = raw if raw is not None else json.dumps(payload or {}).encode("utf-8")
    return httpmod.HttpResponse(status, headers or {}, body)


# A public address for the injected resolver. Without injection the deploy-hook
# guard did a real DNS lookup, so the "no network" suite needed the network.
PUBLIC_IP = "93.184.216.34"


def public_resolver(host):
    return [PUBLIC_IP]


class FakeTransport(httpmod.Transport):
    """Records every call and answers from a queue. Nothing leaves the process."""

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


class ConfigTests(unittest.TestCase):
    def test_empty_environment_reports_everything_missing(self):
        cfg = cfgmod.load({})
        self.assertFalse(cfg.github.configured)
        self.assertFalse(cfg.vercel.configured)
        self.assertFalse(cfg.owner.configured)
        self.assertEqual(cfg.github.missing(), ("GITHUB_TOKEN", "GITHUB_REPO"))
        self.assertEqual(cfg.vercel.missing(),
                         ("VERCEL_TOKEN", "VERCEL_PROJECT_ID", "DEPLOY_HOOK_URL"))
        self.assertEqual(cfg.owner.missing(), ("WAHA_OWNER_TOKEN",))

    def test_load_reads_every_variable(self):
        cfg = cfgmod.load(FULL_ENV)
        self.assertTrue(cfg.github.configured and cfg.vercel.configured
                        and cfg.owner.configured)
        self.assertEqual(cfg.github.repo, "acme/widgets")
        self.assertEqual(cfg.vercel.team_id, "team_9")
        self.assertTrue(cfg.vercel.hook_configured)

    def test_describe_never_contains_a_secret(self):
        dumped = json.dumps(cfgmod.load(FULL_ENV).describe())
        for secret in (OWNER_TOKEN, GITHUB_TOKEN, VERCEL_TOKEN, HOOK_URL):
            self.assertNotIn(secret, dumped)
        # ...but it does name the variables, so an operator can fix the deploy.
        self.assertIn("GITHUB_TOKEN", json.dumps(cfgmod.load({}).describe()))

    def test_secrets_include_the_deploy_hook_url(self):
        # The hook URL *is* the credential for Vercel deploy hooks; omitting it
        # from the redactor list would leak the ability to deploy.
        self.assertIn(HOOK_URL, cfgmod.load(FULL_ENV).secrets())

    def test_session_ttl_defaults_to_eight_hours(self):
        self.assertEqual(cfgmod.load({}).owner.session_ttl_seconds, 8 * 3600)

    def test_write_limit_defaults_to_three_per_minute(self):
        self.assertEqual(cfgmod.load({}).owner.write_limit_per_minute, 3)

    def test_out_of_range_numbers_are_clamped_not_trusted(self):
        cfg = cfgmod.load({"WAHA_OWNER_SESSION_TTL": "999999999",
                           "WAHA_OWNER_WRITE_LIMIT_PER_MINUTE": "0",
                           "WAHA_INTEGRATIONS_PAGE_SIZE": "100000"})
        self.assertEqual(cfg.owner.session_ttl_seconds, 24 * 3600)
        self.assertEqual(cfg.owner.write_limit_per_minute, 1)
        self.assertEqual(cfg.page_size, cfgmod.MAX_PAGE_SIZE)

    def test_unparsable_number_falls_back_to_the_default(self):
        self.assertEqual(cfgmod.load({"WAHA_OWNER_SESSION_TTL": "abc"})
                         .owner.session_ttl_seconds, 8 * 3600)

    def test_origins_are_split_and_trailing_slashes_stripped(self):
        cfg = cfgmod.load({"WAHA_OWNER_ALLOWED_ORIGINS": "https://a.test/, https://b.test"})
        self.assertEqual(cfg.owner.allowed_origins,
                         frozenset({"https://a.test", "https://b.test"}))

    def test_origins_are_canonicalized_and_invalid_entries_are_dropped(self):
        cfg = cfgmod.load({"WAHA_OWNER_ALLOWED_ORIGINS":
                           "HTTPS://Admin.Test:443/, https://user@evil.test, "
                           "https://admin.test/path, https://127.1"})
        self.assertEqual(cfg.owner.allowed_origins, frozenset({"https://admin.test"}))
        self.assertEqual(cfgmod.normalize_origin("http://localhost:80/"),
                         "http://localhost")

    def test_trusted_hosts_include_exact_manual_platform_and_origin_hosts(self):
        hosts = set(cfgmod.trusted_hosts({
            "WAHA_TRUSTED_HOSTS": "custom.example.test,*.attacker.test",
            "RENDER_EXTERNAL_HOSTNAME": "waha.onrender.com",
            "VERCEL_URL": "release.vercel.app",
            "WAHA_ALLOWED_ORIGINS": "https://pages.example.test",
            "WAHA_OWNER_ALLOWED_ORIGINS": "https://admin.example.test",
        }))
        self.assertTrue({"custom.example.test", "waha.onrender.com", "release.vercel.app",
                         "pages.example.test", "admin.example.test", "localhost",
                         "127.0.0.1"}.issubset(hosts))
        self.assertFalse(any("*" in host for host in hosts))


class RedactTests(unittest.TestCase):
    def test_every_known_secret_is_replaced(self):
        redact = redactmod.build_redactor(cfgmod.load(FULL_ENV).secrets())
        out = redact(f"token {GITHUB_TOKEN} and hook {HOOK_URL} and {VERCEL_TOKEN}")
        self.assertNotIn(GITHUB_TOKEN, out)
        self.assertNotIn(HOOK_URL, out)
        self.assertNotIn(VERCEL_TOKEN, out)
        self.assertEqual(out.count(redactmod.REDACTED), 3)

    def test_values_too_short_to_be_credentials_are_left_alone(self):
        # Otherwise a 3-character "secret" would redact ordinary words out of an
        # error message and make the message useless to the operator.
        redact = redactmod.build_redactor(["abc"])
        self.assertEqual(redact("abc def"), "abc def")

    def test_the_longest_secret_is_removed_first(self):
        # If the token were replaced first, the hook URL that embeds it would be
        # left behind with a hole in it -- still a partial leak of the URL.
        inner = "x" * 20
        outer = f"https://hook.test/{inner}/tail"
        redact = redactmod.build_redactor([inner, outer])
        self.assertEqual(redact(outer), redactmod.REDACTED)

    def test_fingerprint_is_stable_and_does_not_contain_the_value(self):
        a = redactmod.fingerprint(GITHUB_TOKEN)
        self.assertEqual(a, redactmod.fingerprint(GITHUB_TOKEN))
        self.assertNotIn(GITHUB_TOKEN, a)
        self.assertNotEqual(redactmod.fingerprint(VERCEL_TOKEN), a)

    def test_fingerprint_of_nothing_is_nothing(self):
        self.assertEqual(redactmod.fingerprint(""), "")


class TransportGuardTests(unittest.TestCase):
    def test_plain_http_is_refused(self):
        with self.assertRaises(httpmod.IntegrationError) as caught:
            httpmod.assert_https_host("http://api.github.com/x",
                                      httpmod.ALLOWED_API_HOSTS)
        self.assertEqual(caught.exception.code, "insecure_target")

    def test_a_host_outside_the_allowlist_is_refused(self):
        with self.assertRaises(httpmod.IntegrationError) as caught:
            httpmod.assert_https_host("https://evil.test/x", httpmod.ALLOWED_API_HOSTS)
        self.assertEqual(caught.exception.code, "host_not_allowed")

    def test_an_allowlisted_host_is_accepted(self):
        self.assertEqual(
            httpmod.assert_https_host("https://api.vercel.com/v6/deployments",
                                      httpmod.ALLOWED_API_HOSTS), "api.vercel.com")

    def test_a_loopback_hook_is_refused(self):
        # A typo'd DEPLOY_HOOK_URL must not be able to point a deploy at the
        # instance's own metadata endpoint.
        with self.assertRaises(httpmod.IntegrationError) as caught:
            httpmod.assert_public_host("hook.test", lambda host: ["127.0.0.1"])
        self.assertEqual(caught.exception.code, "internal_address_blocked")

    def test_a_private_range_hook_is_refused(self):
        # 169.254.169.254 is the cloud metadata address; 10.x is the VPC.
        for address in ("169.254.169.254", "10.0.0.5", "192.168.1.1"):
            with self.subTest(address=address):
                with self.assertRaises(httpmod.IntegrationError):
                    httpmod.assert_public_host("hook.test", lambda host, a=address: [a])

    def test_a_public_hook_address_is_accepted(self):
        self.assertTrue(httpmod.assert_public_host("hook.test", lambda host: [PUBLIC_IP]))

    def test_dns_results_must_be_nonempty_and_every_answer_public(self):
        with self.assertRaises(httpmod.IntegrationError) as caught:
            httpmod.resolve_public_addresses("hook.test", lambda host: [])
        self.assertEqual(caught.exception.code, "dns_failed")
        with self.assertRaises(httpmod.IntegrationError) as caught:
            httpmod.resolve_public_addresses("hook.test", lambda host: [PUBLIC_IP, "10.0.0.4"])
        self.assertEqual(caught.exception.code, "internal_address_blocked")

    def test_checked_dns_answers_are_canonical_and_deduplicated(self):
        addresses = httpmod.resolve_public_addresses(
            "hook.test", lambda host: [PUBLIC_IP, PUBLIC_IP, "2606:4700:4700::1111"])
        self.assertEqual(addresses, (PUBLIC_IP, "2606:4700:4700::1111"))

    def test_https_urls_reject_credentials_fragments_and_nonstandard_ports(self):
        for url in ("https://user@api.github.com/x", "https://api.github.com:8443/x",
                    "https://api.github.com/x#fragment"):
            with self.subTest(url=url), self.assertRaises(httpmod.IntegrationError) as caught:
                httpmod.assert_https_host(url, httpmod.ALLOWED_API_HOSTS)
            self.assertEqual(caught.exception.code, "invalid_target")

    def test_pinned_https_connection_dials_ip_but_keeps_hostname_for_tls(self):
        raw_socket, wrapped_socket = Mock(), Mock()
        context = Mock()
        context.verify_mode = ssl.CERT_REQUIRED
        context.check_hostname = True
        context.wrap_socket.return_value = wrapped_socket
        connection = httpmod._PinnedHTTPSConnection(
            "hook.example.test", 443, [PUBLIC_IP], timeout=7, context=context)
        with patch("integrations.http.socket.create_connection", return_value=raw_socket) as dial:
            connection.connect()
        dial.assert_called_once_with((PUBLIC_IP, 443), 7)
        context.wrap_socket.assert_called_once_with(raw_socket,
                                                   server_hostname="hook.example.test")
        self.assertIs(connection.sock, wrapped_socket)
        connection.close()

    def test_urllib_transport_uses_the_checked_ip_list_without_host_header_override(self):
        with patch("integrations.http._PinnedHTTPSConnection") as connection_type:
            connection = connection_type.return_value
            upstream = Mock(status=202)
            upstream.getheaders.return_value = [("Content-Type", "application/json")]
            upstream.read.return_value = b"{}"
            connection.getresponse.return_value = upstream
            result = httpmod.UrllibTransport().request(
                "POST", "https://hook.example.test/deploy?source=waha",
                headers={"Host": "evil.example.test", "User-Agent": "test"},
                body=b"{}", resolved_addresses=[PUBLIC_IP])
        connection_type.assert_called_once_with("hook.example.test", 443,
                                                (PUBLIC_IP,), timeout=15)
        connection.request.assert_called_once_with("POST", "/deploy?source=waha",
                                                   body=b"{}", headers={"User-Agent": "test"})
        self.assertEqual(result.status, 202)

    def test_a_dead_connection_is_graded_by_when_it_failed_not_by_how_loud_it_looked(self):
        """A failure with no HTTP response is not a verdict on the credential.

        The sandbox case that motivated this: an egress filter closes the TLS
        handshake, so the token is never sent and never answered. Every code below
        lands in ``NETWORK_FAILURE_CODES``, which is what the live checker reads to
        report BLOCKED -- reporting FAIL there would blame a working token for a
        network policy, and the two have opposite fixes.
        """
        cases = ((ssl.SSLZeroReturnError("TLS/SSL connection has been closed (EOF)"),
                  "egress_blocked"),
                 (ConnectionResetError("reset by peer"), "egress_blocked"),
                 (socket.timeout("no answer"), "upstream_timeout"),
                 (OSError("generic transport failure"), "upstream_unreachable"))
        for error, expected in cases:
            with self.subTest(error=type(error).__name__), \
                    patch("integrations.http._PinnedHTTPSConnection") as connection_type:
                connection_type.return_value.request.side_effect = error
                with self.assertRaises(httpmod.IntegrationError) as caught:
                    httpmod.UrllibTransport().request(
                        "GET", "https://hook.example.test/deploy",
                        resolved_addresses=[PUBLIC_IP])
                self.assertEqual(caught.exception.code, expected)
                self.assertIn(caught.exception.code, httpmod.NETWORK_FAILURE_CODES)
        # The inverse is the whole point of the set: an answered 401 is a credential
        # verdict, and no status-bearing code may be excused as a network problem.
        for code in ("ghp_unauthorized", "github_forbidden", "vercel_not_found",
                     "github_error", "vercel_error"):
            self.assertNotIn(code, httpmod.NETWORK_FAILURE_CODES)

    def test_retry_after_is_read_only_for_throttle_statuses(self):
        self.assertEqual(httpmod.retry_after_seconds(response(429, headers={"Retry-After": "30"})), 30)
        self.assertEqual(httpmod.retry_after_seconds(response(503, headers={"Retry-After": "5"})), 5)
        self.assertIsNone(httpmod.retry_after_seconds(response(400, headers={"Retry-After": "30"})))
        self.assertIsNone(httpmod.retry_after_seconds(response(429, headers={"Retry-After": "soon"})))

    def test_header_lookup_ignores_case(self):
        self.assertEqual(response(200, headers={"Content-TYPE": "application/json"})
                         .header("content-type"), "application/json")

    def test_unparsable_body_is_none_not_an_exception(self):
        self.assertIsNone(response(200, raw=b"<html>nope</html>").json())


class GitHubClientTests(unittest.TestCase):
    def setUp(self):
        self.settings = cfgmod.load(FULL_ENV).github
        self.redact = redactmod.build_redactor(cfgmod.load(FULL_ENV).secrets())

    def client(self, *answers):
        transport = FakeTransport(*answers)
        return GitHubClient(self.settings, self.redact, transport=transport), transport

    def test_runs_are_normalised(self):
        client, _ = self.client(response(200, {
            "total_count": 2,
            "workflow_runs": [
                {"id": 1, "run_number": 9, "name": "CI", "display_title": "fix: x",
                 "status": "completed", "conclusion": "success", "head_branch": "main",
                 "head_sha": "a" * 40, "html_url": "https://github.com/a/b/actions/runs/1",
                 "created_at": "2026-01-01T00:00:00Z", "event": "push"}]}))
        out = client.list_runs(limit=5)
        self.assertEqual(out["count"], 1)
        self.assertEqual(out["items"][0]["sha"], "a" * 12)
        self.assertEqual(out["items"][0]["conclusion"], "success")
        self.assertEqual(out["source"], "GITHUB_API")

    def test_the_commit_author_email_never_reaches_the_response(self):
        client, _ = self.client(response(200, {
            "workflow_runs": [{"id": 1, "head_commit": {"author": {"email": "dev@corp.test"}},
                               "status": "completed", "conclusion": "success"}]}))
        self.assertNotIn("dev@corp.test", json.dumps(client.list_runs()))

    def test_a_malformed_repo_setting_is_refused_before_any_call(self):
        client, transport = self.client()
        client.settings = cfgmod.load(dict(FULL_ENV, GITHUB_REPO="no-slash")).github
        with self.assertRaises(httpmod.IntegrationError) as caught:
            client.list_runs()
        self.assertEqual(caught.exception.code, "invalid_config")
        self.assertEqual(transport.calls, [])

    def test_dispatch_sends_the_ref_and_the_api_version(self):
        client, transport = self.client(response(204, raw=b""))
        out = client.dispatch(ref="release")
        self.assertTrue(out["dispatched"])
        call = transport.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertTrue(call["url"].endswith("/actions/workflows/ci.yml/dispatches"))
        # The transport owns JSON encoding, so the client hands it a dict. Asserted
        # as a dict: if a client ever started pre-encoding, the fake would see bytes
        # and this would fail rather than silently passing on a different shape.
        self.assertEqual(call["body"], {"ref": "release"})
        self.assertEqual(call["headers"]["X-GitHub-Api-Version"], "2022-11-28")

    def test_dispatch_without_a_workflow_is_a_config_error(self):
        client, transport = self.client()
        client.settings = cfgmod.load(dict(FULL_ENV, GITHUB_WORKFLOW_ID="")).github
        with self.assertRaises(httpmod.IntegrationError) as caught:
            client.dispatch()
        self.assertEqual(caught.exception.code, "invalid_config")
        self.assertEqual(transport.calls, [])

    def test_a_401_is_reported_as_an_authorisation_problem(self):
        client, _ = self.client(response(401, {"message": "Bad credentials"}))
        with self.assertRaises(httpmod.IntegrationError) as caught:
            client.list_runs()
        self.assertEqual(caught.exception.code, "ghp_unauthorized")
        self.assertEqual(caught.exception.status, 401)

    def test_an_upstream_message_cannot_carry_the_token_out(self):
        client, _ = self.client(response(422, {"message": f"rejected {GITHUB_TOKEN}"}))
        with self.assertRaises(httpmod.IntegrationError) as caught:
            client.dispatch()
        self.assertNotIn(GITHUB_TOKEN, caught.exception.message)
        self.assertIn(redactmod.REDACTED, caught.exception.message)

    def test_a_throttled_upstream_carries_retry_after(self):
        client, _ = self.client(response(429, {"message": "slow down"},
                                         headers={"Retry-After": "42"}))
        with self.assertRaises(httpmod.IntegrationError) as caught:
            client.list_runs()
        self.assertEqual(caught.exception.code, "github_unavailable")
        self.assertEqual(caught.exception.retry_after, 42)

    def test_an_unconfigured_integration_is_a_503_not_a_400(self):
        client, transport = self.client()
        client.settings = cfgmod.load({}).github
        with self.assertRaises(httpmod.IntegrationError) as caught:
            client.list_runs()
        self.assertEqual(caught.exception.code, "not_configured")
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(transport.calls, [])

    def test_a_body_that_is_not_the_documented_shape_is_refused(self):
        client, _ = self.client(response(200, {"unexpected": True}))
        with self.assertRaises(httpmod.IntegrationError) as caught:
            client.list_runs()
        self.assertEqual(caught.exception.code, "bad_upstream_payload")


class VercelClientTests(unittest.TestCase):
    def setUp(self):
        cfg = cfgmod.load(FULL_ENV)
        self.settings = cfg.vercel
        self.redact = redactmod.build_redactor(cfg.secrets())

    def client(self, *answers):
        transport = FakeTransport(*answers)
        return (VercelClient(self.settings, self.redact, transport=transport,
                             resolver=public_resolver), transport)

    def test_deployments_are_normalised(self):
        client, _ = self.client(response(200, {
            "deployments": [{"uid": "dpl_1", "name": "widgets", "url": "widgets-x.vercel.app",
                             "state": "READY", "target": "production", "created": 1760000000000,
                             "creator": {"username": "owner"},
                             "meta": {"githubCommitRef": "main",
                                      "githubCommitSha": "b" * 40,
                                      "internalField": "not for the browser"}}],
            "pagination": {"next": 1760000000000}}))
        out = client.list_deployments(limit=5)
        self.assertEqual(out["count"], 1)
        row = out["items"][0]
        self.assertEqual(row["sha"], "b" * 12)
        self.assertEqual(row["branch"], "main")
        self.assertTrue(out["has_more"])
        self.assertNotIn("not for the browser", json.dumps(out))

    def test_the_project_and_team_reach_the_query_string(self):
        client, transport = self.client(response(200, {"deployments": []}))
        client.list_deployments(limit=7)
        self.assertIn("projectId=prj_123", transport.calls[0]["url"])
        self.assertIn("teamId=team_9", transport.calls[0]["url"])
        self.assertIn("limit=7", transport.calls[0]["url"])

    def test_the_hook_answer_is_reduced_to_an_id_and_a_state(self):
        client, transport = self.client(response(200, {
            "id": "dpl_9", "url": "widgets.vercel.app", "state": "QUEUED",
            "env": {"SECRET_SHOULD_NOT_TRAVEL": "hunter2"}}))
        out = client.trigger_deploy_hook()
        self.assertTrue(out["triggered"])
        self.assertEqual(out["deployment_id"], "dpl_9")
        self.assertEqual(out["source"], "DEPLOY_HOOK")
        self.assertNotIn("hunter2", json.dumps(out))
        self.assertEqual(transport.calls[0]["method"], "POST")

    def test_hook_dns_is_resolved_once_and_the_checked_addresses_are_pinned(self):
        answers = []

        def rebinding_resolver(host):
            answers.append(host)
            return [PUBLIC_IP] if len(answers) == 1 else ["127.0.0.1"]

        transport = FakeTransport(response(200, {"id": "dpl_pinned"}))
        client = VercelClient(self.settings, self.redact, transport=transport,
                              resolver=rebinding_resolver)
        result = client.trigger_deploy_hook()
        self.assertEqual(result["deployment_id"], "dpl_pinned")
        self.assertEqual(answers, ["api.vercel.com"], "DNS must not be queried again after validation")
        self.assertEqual(transport.calls[0]["resolved_addresses"], (PUBLIC_IP,))

    def test_a_non_vercel_hook_host_is_refused_before_dns_or_transport(self):
        client, transport = self.client()
        client.settings = cfgmod.load(dict(
            FULL_ENV, DEPLOY_HOOK_URL="https://attacker.example.test/deploy")).vercel
        with self.assertRaises(httpmod.IntegrationError) as caught:
            client.trigger_deploy_hook()
        self.assertEqual(caught.exception.code, "host_not_allowed")
        self.assertEqual(transport.calls, [])

    def test_a_hook_on_plain_http_is_refused(self):
        client, transport = self.client()
        client.settings = cfgmod.load(dict(FULL_ENV,
                                           DEPLOY_HOOK_URL="http://hook.test/x")).vercel
        with self.assertRaises(httpmod.IntegrationError) as caught:
            client.trigger_deploy_hook()
        self.assertEqual(caught.exception.code, "insecure_target")
        self.assertEqual(transport.calls, [])

    def test_a_missing_hook_is_a_503(self):
        client, transport = self.client()
        client.settings = cfgmod.load(dict(FULL_ENV, DEPLOY_HOOK_URL="")).vercel
        with self.assertRaises(httpmod.IntegrationError) as caught:
            client.trigger_deploy_hook()
        self.assertEqual(caught.exception.code, "not_configured")
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(transport.calls, [])

    def test_vercels_error_envelope_is_read_from_its_nested_shape(self):
        client, _ = self.client(response(403, {"error": {"code": "forbidden",
                                                        "message": "no access"}}))
        with self.assertRaises(httpmod.IntegrationError) as caught:
            client.list_deployments()
        self.assertEqual(caught.exception.code, "vercel_forbidden")
        self.assertIn("no access", caught.exception.message)


class ServiceTests(unittest.TestCase):
    def service(self, env=FULL_ENV, *answers):
        cfg = cfgmod.load(env)
        transport = FakeTransport(*answers)
        return (IntegrationService(cfg, transport=transport,
                                   resolver=public_resolver), transport)

    def test_capabilities_follow_the_config_not_the_page_load(self):
        service, _ = self.service()
        status = service.status()
        self.assertTrue(status["github"]["capabilities"]["list_runs"])
        self.assertTrue(status["github"]["capabilities"]["dispatch"])
        self.assertTrue(status["vercel"]["capabilities"]["trigger_hook"])

    def test_a_missing_workflow_disables_dispatch_only(self):
        service, _ = self.service(dict(FULL_ENV, GITHUB_WORKFLOW_ID=""))
        caps = service.status()["github"]["capabilities"]
        self.assertTrue(caps["list_runs"])
        self.assertFalse(caps["dispatch"])

    def test_nothing_configured_means_no_capability_is_claimed(self):
        service, _ = self.service({})
        status = service.status()
        for block in (status["github"], status["vercel"]):
            self.assertFalse(any(block["capabilities"].values()))

    def test_the_status_payload_is_secret_free(self):
        service, _ = self.service()
        dumped = json.dumps(service.status())
        for secret in (OWNER_TOKEN, GITHUB_TOKEN, VERCEL_TOKEN, HOOK_URL):
            self.assertNotIn(secret, dumped)

    def test_reads_are_stamped_with_when_they_happened(self):
        service, _ = self.service(FULL_ENV, response(200, {"workflow_runs": []}))
        self.assertIn("fetched_at", service.github_runs())

    def test_the_confirm_phrases_are_published_to_the_client(self):
        service, _ = self.service()
        self.assertEqual(service.status()["confirm_phrases"],
                         {"github_dispatch": "dispatch-ci", "vercel_deploy": "deploy"})


class ModuleImportTests(unittest.TestCase):
    def test_the_package_exports_what_app_py_imports(self):
        # Imported as a real package, not by file path: loading `__init__.py` under
        # a made-up name breaks its own relative imports and proves nothing.
        import integrations
        for name in ("IntegrationConfig", "load", "IntegrationService",
                     "Transport", "UrllibTransport", "IntegrationError",
                     "HttpResponse", "build_redactor", "REDACTED", "fingerprint"):
            self.assertTrue(hasattr(integrations, name), f"integrations lost `{name}`")


if __name__ == "__main__":
    unittest.main()
