"""Guards for the Vercel deployment config.

These assert the invariants that a bad edit silently breaks: a vercel.json that
Vercel's schema rejects, an entry point that stops exposing a WSGI callable, and
a root requirements.txt that loses psycopg's bundled libpq.
"""
import importlib.util
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Importing the app creates its data directory and a csrf secret sidecar; keep
# both in a scratch directory regardless of which test module loads first.
_scratch = tempfile.TemporaryDirectory()
os.environ.setdefault("WAHA_DB", str(Path(_scratch.name) / "waha-vercel-test.db"))
os.environ.setdefault("WAHA_ALLOWED_ORIGINS", "https://pages.test")


def _load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class VercelConfigTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((ROOT / "vercel.json").read_text())

    def test_vercel_json_is_schema_valid_and_hobby_safe(self):
        # `functions` and `builds` are mutually exclusive; sending both makes
        # Vercel fail the build with "Conflicting functions and builds".
        # subTest keeps the independent checks isolated so one bad edit reports
        # every violation instead of stopping at the first.
        with self.subTest("no legacy builds property"):
            self.assertNotIn("builds", self.config)
        with self.subTest("functions declared"):
            self.assertIn("functions", self.config)
        # excludeFiles is only valid inside `functions`, never at the top level.
        with self.subTest("excludeFiles not at top level"):
            self.assertNotIn("excludeFiles", self.config)

        function = self.config.get("functions", {}).get("api/index.py", {})
        # 60s is the ceiling every Hobby project honours; 300s only applies
        # where Fluid Compute is enabled.
        with self.subTest("maxDuration within Hobby ceiling"):
            self.assertLessEqual(function.get("maxDuration", 0), 60)
        with self.subTest("excludeFiles inside functions"):
            self.assertIn("excludeFiles", function)
        # Handoff artifacts must never ship inside the function bundle.
        with self.subTest("_handoff excluded from bundle"):
            self.assertIn("_handoff/**", function.get("excludeFiles", ""))

        # The rewrite is what lets Flask own routes other than /api/*.
        with self.subTest("rewrite targets the entry point"):
            self.assertEqual(
                self.config.get("rewrites", [{}])[0].get("destination"), "/api/index")

    def test_entrypoints_expose_the_same_wsgi_app(self):
        wsgi = _load("wsgi_entrypoint", "wsgi.py")
        entrypoint = _load("api_index_entrypoint", "api/index.py")

        self.assertTrue(callable(wsgi.application))
        self.assertTrue(callable(entrypoint.app))
        self.assertTrue(callable(entrypoint.application))
        # Both entry points must serve one Flask app, not two separate copies
        # with divergent configuration.
        self.assertIs(wsgi.application, entrypoint.app)
        self.assertEqual(entrypoint.app.name, "backend.app")

    def test_root_requirements_delegate_and_keep_binary_psycopg(self):
        root = (ROOT / "requirements.txt").read_text()
        backend = (ROOT / "backend/requirements.txt").read_text()

        # One source of truth, so Render and Vercel cannot drift.
        self.assertIn("-r backend/requirements.txt", root)

        # A bare psycopg pin drops the bundled libpq that the serverless image
        # has no system copy of.
        bare = [line for line in root.splitlines()
                if re.match(r"\s*psycopg\s*[=~<>]", line)]
        self.assertEqual(bare, [], f"root requirements.txt re-pins psycopg: {bare}")
        self.assertIn("psycopg[binary]", backend)


if __name__ == "__main__":
    unittest.main()
