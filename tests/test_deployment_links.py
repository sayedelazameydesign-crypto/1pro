"""The publisher of deployment links must not be able to invent one.

`scripts/deployment_links.py` exists because a hostname was typed into prose and
the typo shipped a 404 to the reader. These tests hold the two properties that
make the script worth having rather than a prettier way to type a hostname: the
self-test proving every printed URL came out of the payload, and the failure
path -- when the API cannot be read, the answer is *nothing*, never a guess.
"""

import contextlib
import importlib.util
import io
import pathlib
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "deployment_links", ROOT / "scripts/deployment_links.py")
links = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(links)


class ThePublisherCannotInventALink(unittest.TestCase):
    def test_its_own_properties_hold(self):
        # The publisher's self-test narrates each property it checks; that narration
        # belongs to whoever runs the script, so it is captured here and shown only
        # if something failed.
        report = io.StringIO()
        with contextlib.redirect_stdout(report):
            code = links.self_test()
        self.assertEqual(code, 0, f"the publisher's self-test failed:\n{report.getvalue()}")

    def test_the_newest_status_wins_whatever_the_order(self):
        statuses = [
            {"state": "pending", "created_at": "2026-10-07T14:23:55Z",
             "environment_url": "https://stale.invalid"},
            {"state": "success", "created_at": "2026-10-07T14:24:00Z",
             "environment_url": "https://newest.invalid"},
        ]
        self.assertEqual(links.newest_status(statuses)["state"], "success")
        self.assertEqual(links.newest_status([]), {})

    def test_an_unreadable_api_prints_nothing_rather_than_a_link(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(links, "collect", side_effect=RuntimeError("offline")):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = links.main(["--limit", "3"])
        self.assertEqual(code, 2, "an unreadable API is an error, not a successful empty table")
        self.assertEqual(stdout.getvalue().strip(), "",
                         "a link was printed although nothing was read")
        self.assertIn("no link is better than an unverified one", stderr.getvalue())

    def test_the_renderer_prints_no_url_the_payload_did_not_carry(self):
        payload = [{"id": 7, "sha": "c" * 40, "ref": "main", "environment": "Preview",
                    "created_at": "2026-10-07T10:00:00Z"}]
        statuses = {7: [{"state": "success", "created_at": "2026-10-07T10:01:00Z",
                         "environment_url": "https://from-the-payload.invalid"}]}
        text = links.render(links.rows(payload, statuses))
        for token in text.split():
            if token.startswith(("http://", "https://")):
                self.assertIn(token, links.extract_urls([payload, statuses]),
                              "the renderer printed a URL that was not in the payload")

    def test_a_deployment_without_a_status_gets_a_placeholder(self):
        payload = [{"id": 8, "sha": "d" * 40, "ref": "main", "environment": "Preview",
                    "created_at": "2026-10-07T10:00:00Z"}]
        rendered = links.render(links.rows(payload, {}))
        self.assertIn(links.NO_URL, rendered)
        self.assertNotIn("http", rendered,
                         "a deployment with nothing recorded must not gain a link")


if __name__ == "__main__":
    unittest.main()
