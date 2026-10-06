"""The deploy doctor must catch the mistakes that actually break a free deploy."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from deploy_doctor import check, load_env_file, self_test  # noqa: E402

PAGES = "https://sayedelazameydesign-crypto.github.io"
GOOD = {
    "DATABASE_URL": "postgresql://u:p@ep-x-pooler.us-east.aws.neon.tech/db?sslmode=require",
    "GEMINI_API_KEY": "AIza" + "Q" * 35,
    "WAHA_SECRET": "s" * 64,
    "WAHA_ALLOWED_ORIGINS": PAGES,
}


def codes(errors):
    return " || ".join(errors)


class EnvFileTests(unittest.TestCase):
    def test_parses_export_quotes_and_comments(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "service.env"
            path.write_text("# comment\nexport GEMINI_API_KEY='abc123'\nWAHA_SECRET=\"q\"\n\n"
                            "EMPTY=\nnot-an-env-line\nDATABASE_URL=postgresql://h/d?sslmode=require\n",
                            encoding="utf-8")
            values = load_env_file(path)
        self.assertEqual(values["GEMINI_API_KEY"], "abc123")
        self.assertEqual(values["WAHA_SECRET"], "q")
        self.assertEqual(values["EMPTY"], "")
        self.assertIn("DATABASE_URL", values)
        self.assertNotIn("not-an-env-line", values)


class CheckTests(unittest.TestCase):
    def test_clean_environment_reports_no_errors(self):
        errors, _warnings, _notes = check(dict(GOOD), target="render")
        self.assertEqual(errors, [])

    def test_spaced_variable_name_is_an_error(self):
        errors, _w, _n = check(dict(GOOD, **{"Gemini API Key": "AIza" + "Q" * 35}), target="render")
        self.assertIn("GEMINI_API_KEY", codes(errors))

    def test_promptql_trust_flag_is_refused_on_a_public_service(self):
        errors, _w, _n = check(dict(GOOD, WAHA_TRUST_PROMPTQL="1"), target="render")
        self.assertIn("WAHA_TRUST_PROMPTQL", codes(errors))

    def test_gateway_url_would_disable_the_direct_key(self):
        errors, _w, _n = check(dict(GOOD, PROMPTQL_PLATFORM_API_URL="https://gw.test"), target="render")
        self.assertIn("PROMPTQL_PLATFORM_API_URL", codes(errors))

    def test_sqlite_and_missing_sslmode_are_errors(self):
        errors, _w, _n = check(dict(GOOD, DATABASE_URL="sqlite:///data/waha.db"), target="render")
        self.assertIn("SQLite", codes(errors))
        errors, _w, _n = check(dict(GOOD, DATABASE_URL="postgresql://u:p@host/db"), target="render")
        self.assertIn("sslmode=require", codes(errors))

    def test_direct_endpoint_only_warns(self):
        direct = dict(GOOD, DATABASE_URL=GOOD["DATABASE_URL"].replace("-pooler", ""))
        errors, warnings, _notes = check(direct, target="render")
        self.assertEqual(errors, [])
        self.assertTrue(any("pooled" in item for item in warnings))

    def test_origin_must_not_carry_a_path(self):
        errors, _w, _n = check(dict(GOOD, WAHA_ALLOWED_ORIGINS=PAGES + "/1pro"), target="render")
        self.assertIn("/1pro", codes(errors))

    def test_missing_pages_origin_is_an_error(self):
        errors, _w, _n = check(dict(GOOD, WAHA_ALLOWED_ORIGINS="https://elsewhere.test"), target="render")
        self.assertIn("WAHA_ALLOWED_ORIGINS", codes(errors))

    def test_placeholder_secrets_are_refused(self):
        for value in ("changeme", "sk-xxxx", "your_key_here", "short"):
            errors, _w, _n = check(dict(GOOD, GEMINI_API_KEY=value), target="render")
            self.assertTrue(errors, value)

    def test_vercel_requires_a_stable_secret(self):
        env = {key: value for key, value in GOOD.items() if key != "WAHA_SECRET"}
        errors, _w, _n = check(env, target="vercel")
        self.assertIn("WAHA_SECRET", codes(errors))
        errors, _w, _n = check(env, target="render")
        self.assertNotIn("WAHA_SECRET", codes(errors))

    def test_agent_config_range_is_reported(self):
        errors, _w, _n = check(dict(GOOD, AGENT_MAX_STEPS="999"), target="render")
        self.assertIn("AGENT_MAX_STEPS", codes(errors))

    def test_fake_agent_mode_is_flagged_on_public_hosts(self):
        _e, warnings, _n = check(dict(GOOD, AGENT_FAKE="1"), target="render")
        self.assertTrue(any("AGENT_FAKE" in item for item in warnings))

    def test_network_tools_note_mentions_approvals(self):
        _e, _w, notes = check(dict(GOOD, AGENT_NETWORK_TOOLS="1"), target="render")
        self.assertTrue(any("web_fetch" in item for item in notes))

    def test_vercel_bundle_keeps_what_the_search_endpoint_reads(self):
        # R2 loads backend/rag_text.py and data/rag/index.json at request time. The
        # bundle ships backend/ and data/ and excludes scripts/**; excluding data/ would
        # make /api/search 503 on Vercel while every repo test stayed green.
        data = json.loads((ROOT / "vercel.json").read_text(encoding="utf-8"))
        raw = data["functions"]["api/index.py"]["excludeFiles"].strip("{}")
        excluded = {item.strip() for item in raw.split(",")}
        self.assertNotIn("data/**", excluded)
        self.assertNotIn("backend/**", excluded)
        self.assertIn("scripts/**", excluded, "the builder stays out of the bundle")
        self.assertTrue((ROOT / "backend" / "rag_text.py").exists())
        self.assertTrue((ROOT / "data" / "rag" / "index.json").exists())

    def test_the_repo_ships_a_vercel_config_that_passes_its_own_rules(self):
        # Verbatim regression guard for the two shapes that break Vercel builds.
        data = json.loads((ROOT / "vercel.json").read_text(encoding="utf-8"))
        self.assertNotIn("builds", data)
        self.assertEqual(data["functions"]["api/index.py"]["maxDuration"], 60)
        errors, warnings, notes = check(dict(GOOD), target="vercel")
        self.assertEqual([item for item in errors if "vercel.json" in item], [])


class SelfTestTests(unittest.TestCase):
    def test_doctor_passes_its_own_fixtures(self):
        self.assertEqual(self_test(), [])


if __name__ == "__main__":
    unittest.main()
