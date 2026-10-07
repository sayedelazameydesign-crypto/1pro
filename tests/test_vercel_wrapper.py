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

        # No catch-all rewrite. Vercel's backend-framework routing hands the function
        # the rewrite *destination*, so the /(.*) -> /api/index we used to ship made
        # Flask see PATH_INFO=/api/index for every request and answer its own 404 on
        # /, /health, /readyz and all of /api/* (observed live on cela-umber.vercel.app).
        # The build already sends every path to the app, so the property stays absent.
        with self.subTest("no catch-all rewrite"):
            self.assertNotIn("rewrites", self.config)

    def test_the_app_owns_the_real_paths_not_the_old_rewrite_destination(self):
        """The whole failure in one place: the router serves the real paths and has no
        route at ``/api/index``, which is the page every visitor saw because the
        rewrite made Flask receive that path for every request."""
        client = _load("api_index_client", "api/index.py").app.test_client()
        for path in ("/", "/health", "/api/skills"):
            with self.subTest(path):
                self.assertEqual(client.get(path).status_code, 200)
        with self.subTest("old rewrite destination"):
            self.assertEqual(client.get("/api/index").status_code, 404)

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

class RootManifestTests(unittest.TestCase):
    """Vercel's builder parses the root manifest itself and cannot follow an `-r`
    include: the build dies with "could not parse requirements.txt: Error parsing
    included file", which is what kept every deployment red. The file is therefore
    flat -- and because a flat file can drift, equality with the file Render installs
    is enforced here rather than trusted.
    """

    @staticmethod
    def pins(text):
        return [line.strip() for line in text.splitlines()
                if line.strip() and not line.strip().startswith("#")]

    def test_the_root_manifest_carries_no_include_directive(self):
        root = self.pins((ROOT / "requirements.txt").read_text())
        includes = [line for line in root
                    if line.startswith(("-r ", "--requirement", "-e "))]
        self.assertEqual(includes, [],
                         f"the root manifest still includes files, and Vercel cannot parse that: {includes}")

    def test_both_manifests_pin_the_same_runtime(self):
        root = self.pins((ROOT / "requirements.txt").read_text())
        backend = self.pins((ROOT / "backend/requirements.txt").read_text())
        self.assertEqual(root, backend,
                         "requirements.txt drifted from backend/requirements.txt; bump both")
        # A bare psycopg pin drops the bundled libpq that the serverless image has no
        # system copy of, and no manifest may carry it.
        for name, lines in (("requirements.txt", root), ("backend/requirements.txt", backend)):
            self.assertTrue(any(line.startswith("psycopg[binary]") for line in lines),
                            f"{name} lost the [binary] extra: {lines}")
            bare = [line for line in lines if re.match(r"psycopg\s*[=~<>]", line)]
            self.assertEqual(bare, [], f"{name} re-pins psycopg without [binary]: {bare}")


if __name__ == "__main__":
    unittest.main()
