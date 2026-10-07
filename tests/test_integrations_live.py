"""Layer 3 — live: real calls to GitHub and Vercel.

Skipped unless the operator asks for it, because a test that reaches a third
party's API is not a test CI can own: it depends on credentials, on rate limits,
and on the upstream not changing shape that morning.

    WAHA_LIVE_INTEGRATIONS=1 python -m unittest tests.test_integrations_live

What this layer adds over the two offline ones is narrow and specific: it proves
the *credentials and their scopes* work. A fine-grained GitHub token missing
``actions: read``, or a Vercel token scoped to the wrong team, is invisible to
every mocked test -- the mock answers 200 no matter what the token says.

Mutations are never fired from here, not even with the flag set. ``workflow
dispatch`` and the deploy hook change real state; those are exercised by
``scripts/integrations_live_check.py --allow-mutations``, run by a human who has
decided that starting a CI run and shipping a deployment right now is what they
want. A test suite must not make that decision on its own.
"""
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))

from integrations import config as cfgmod          # noqa: E402
from integrations import http as httpmod           # noqa: E402
from integrations import redact as redactmod       # noqa: E402
from integrations.service import IntegrationService  # noqa: E402
import integrations_live_check as live_check       # noqa: E402

LIVE = os.environ.get("WAHA_LIVE_INTEGRATIONS", "") == "1"


def missing_credentials():
    config = cfgmod.load()
    absent = []
    if not config.github.configured:
        absent.extend(config.github.missing())
    if not config.vercel.configured:
        absent.extend(name for name in config.vercel.missing()
                      if name != "DEPLOY_HOOK_URL")
    return absent


class LiveGateTests(unittest.TestCase):
    """These run always: the gate itself is the thing most likely to break, and a
    live test that silently runs in CI (or silently never runs anywhere) is the
    failure this class pins."""

    def test_the_checker_skips_mutations_unless_asked(self):
        skipped = {item.operation: item.status
                   for item in live_check.skip_mutations()}
        self.assertEqual(skipped, {"workflow dispatch": "SKIP", "deploy hook": "SKIP"})
        for item in live_check.skip_mutations():
            self.assertIn("allow-mutations", item.detail,
                          "a skipped mutation must say how to un-skip it")

    def test_a_run_that_verified_nothing_does_not_report_success(self):
        empty = cfgmod.load({})
        results = live_check.run(config=empty,
                                 service=IntegrationService(empty))
        self.assertTrue(results)
        self.assertFalse(any(item.status == "PASS" for item in results),
                         "an empty run claimed a passing check")

    def test_a_network_failure_is_blocked_and_never_blames_the_token(self):
        """The checker's verdict is about the credential, so a call that died before
        any HTTP response must not produce one. Offline on purpose: a fake transport
        raises what the real one raises when a connection ends before a response, so
        the classification is pinned without depending on any network arrangement.
        """
        config = cfgmod.load({"GITHUB_TOKEN": "ghp_fakeToken12345",
                              "GITHUB_REPO": "acme/widgets",
                              "GITHUB_WORKFLOW_ID": "ci.yml",
                              "VERCEL_TOKEN": "vercel_fakeToken",
                              "VERCEL_PROJECT_ID": "prj_1"})

        class NoResponse(httpmod.Transport):
            def request(self, method, url, headers=None, body=None, timeout=15,
                        resolved_addresses=None):
                raise httpmod.IntegrationError("TLS/SSL connection has been closed (EOF)",
                                               code="pre_http_network_failure")

        service = IntegrationService(config, transport=NoResponse())
        redact = redactmod.build_redactor(config.secrets())
        results = live_check.check_reads(service, redact)
        statuses = {item.operation: item.status for item in results}
        self.assertEqual(statuses["GET workflow runs"], "BLOCKED")
        self.assertEqual(statuses["GET deployments"], "BLOCKED")
        # The code has to survive into the report, or the operator reads a BLOCKED
        # line and still cannot tell a dead connection from an expired token.
        self.assertIn("pre_http_network_failure", live_check.render(results))
        self.assertNotIn("FAIL", live_check.render(results))
        # ...and the inverse: an answered 401 is a verdict and stays one.
        self.assertFalse(live_check.network_blocked(
            httpmod.IntegrationError("Bad credentials", code="ghp_unauthorized")))

    def test_exit_codes_are_precedence_not_independent_numbers(self):
        """Exit 2 means "no check was attempted", so it must never swallow a run
        where a credential went unverified.

        The regression this pins: the first version tested `not passed` before
        anything else, so an all-blocked run -- nothing attempted successfully,
        nothing answered, one credential unjudged -- exited 2, which reads as
        "nothing was configured". The CI log looked clean while a token sat
        unverified, which is the one outcome this checker exists to prevent.
        """

        def statuses(*codes):
            return [live_check.Result("GitHub", "GET workflow runs", code)
                    for code in codes]

        for codes, expected in (((live_check.PASS,), 0),
                                ((live_check.PASS, live_check.SKIP), 0),
                                ((live_check.FAIL,), 1),
                                ((live_check.FAIL, live_check.BLOCKED), 1),
                                ((live_check.BLOCKED,), 3),
                                ((live_check.BLOCKED, live_check.SKIP), 3),
                                ((live_check.PASS, live_check.BLOCKED), 3),
                                ((live_check.SKIP,), 2)):
            with self.subTest(codes=codes):
                self.assertEqual(live_check.exit_code(statuses(*codes)), expected)
        # Only SKIPs produce 2, and any real attempt outranks a skip.
        self.assertNotEqual(live_check.exit_code(statuses(live_check.BLOCKED)), 2)

    def test_the_live_layer_is_off_by_default(self):
        # If this ever fails, someone exported WAHA_LIVE_INTEGRATIONS into CI, and
        # the suite started depending on credentials and upstream availability.
        self.assertFalse(LIVE and bool(os.environ.get("CI")),
                         "WAHA_LIVE_INTEGRATIONS must not be set in CI")


@unittest.skipUnless(LIVE, "set WAHA_LIVE_INTEGRATIONS=1 to run the live checks")
class LiveCredentialTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        absent = missing_credentials()
        if absent:
            raise unittest.SkipTest("missing " + ", ".join(sorted(set(absent))))
        cls.config = cfgmod.load()
        cls.service = IntegrationService(cls.config)
        cls.redact = redactmod.build_redactor(cls.config.secrets())

    def assertNoSecret(self, text):
        for secret in self.config.secrets():
            if secret and len(secret) >= redactmod.MIN_SECRET_LENGTH:
                self.assertNotIn(secret, text)

    def test_github_workflow_runs_are_readable(self):
        results = {item.operation: item
                   for item in live_check.check_reads(self.service, self.redact)}
        item = results["GET workflow runs"]
        self.assertNoSecret(item.detail)
        self.assertEqual(item.status, "PASS", self.redact(item.detail))

    def test_vercel_deployments_are_readable(self):
        results = {item.operation: item
                   for item in live_check.check_reads(self.service, self.redact)}
        item = results["GET deployments"]
        self.assertNoSecret(item.detail)
        self.assertEqual(item.status, "PASS", self.redact(item.detail))

    def test_the_live_report_carries_no_secret(self):
        results = live_check.check_reads(self.service, self.redact)
        self.assertNoSecret(live_check.render(results))
        self.assertNoSecret(live_check.render(results, as_json=True))


if __name__ == "__main__":
    unittest.main()
