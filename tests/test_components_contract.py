"""The system-components panel must report state, not decorate a page.

The panel exists because a status card is the easiest place to lie by accident: six green
dots look identical whether they were read from a server or typed into a template. This
file guards the three ways that can happen here -- a page that draws cards without asking
the server, logic that reaches into the DOM (so no harness can drive it), and a card that
goes green while its own payload is missing.

The behavioural half lives in tests/browser_components.test.mjs and runs in CI next to the
R2 browser contract; this file proves the page actually uses that module and that the
published count still matches the harness.
"""
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGE = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")
APP = (ROOT / "docs" / "assets" / "app.js").read_text(encoding="utf-8")
MODULE = (ROOT / "docs" / "assets" / "components.js").read_text(encoding="utf-8")
PLAN = (ROOT / "RAG-FREE-PLAN.md").read_text(encoding="utf-8")

HEALTH_ENDPOINT = "remote('/health')"
CONFIG_ENDPOINT = "remote('/api/agent/config')"


class PanelIsWiredToTheServer(unittest.TestCase):
    def test_the_page_loads_the_module_before_the_app(self):
        order = [PAGE.find("./assets/search.js"), PAGE.find("./assets/components.js"),
                 PAGE.find("./assets/app.js")]
        self.assertNotIn(-1, order, "the page lost one of its three deferred scripts")
        self.assertEqual(order, sorted(order),
                         "components.js must be defined before app.js runs")

    def test_the_panel_container_exists_and_states_its_source(self):
        for marker in ('id="components-grid"', 'id="components-chip"', 'id="components-foot"'):
            self.assertIn(marker, PAGE, f"the panel lost {marker}")
        self.assertIn("/health", PAGE)
        self.assertIn("/api/agent/config", PAGE)

    def test_the_page_renders_only_through_the_shared_logic(self):
        self.assertIn("window.WahaComponents.build", APP,
                      "the page must ask the module for cards instead of building them inline")
        self.assertIn("built.cards.map", APP)

    def test_the_panel_asks_the_servers_own_endpoints(self):
        self.assertIn(HEALTH_ENDPOINT, APP)
        self.assertIn(CONFIG_ENDPOINT, APP)
        # No third-party status API, and no absolute URL to compare against: both would
        # turn a local deployment fact into someone else's uptime page.
        for text, name in ((APP, "app.js"), (MODULE, "components.js")):
            self.assertNotIn("http://", text, name)
            self.assertNotIn("https://", text, name)

    def test_the_panel_does_not_ship_a_status_word_for_services_it_never_asked_about(self):
        for invented in ("Claude", "Manus", "OpenAI", "Anthropic"):
            self.assertNotIn(invented, PAGE, "a component card may not name a model this app does not use")
            self.assertNotIn(invented, MODULE)


class LogicStaysTestable(unittest.TestCase):
    def test_the_module_touches_no_dom_and_no_network(self):
        for forbidden in ("document.", "innerHTML", "addEventListener", "fetch(", "localStorage"):
            self.assertNotIn(forbidden, MODULE,
                             f"components.js reaches into {forbidden}, so node cannot drive it")

    def test_unknown_is_a_declared_state_and_not_a_borrowed_ready(self):
        self.assertIn("unknown: 'غير معروف'", MODULE)
        # The wording of a missing payload, in one place, so no card can quietly inherit
        # a neighbour's green.
        self.assertIn("NOT_READ", MODULE)
        self.assertIn("غير متصل", MODULE)


class HarnessIsPublished(unittest.TestCase):
    def test_the_components_harness_reports_the_documented_number(self):
        if shutil.which("node") is None:
            self.skipTest("node is not installed")
        result = subprocess.run(["node", str(ROOT / "tests" / "browser_components.test.mjs")],
                                capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr[-400:])
        claimed = re.search(r"system components contract: ok \((\d+) checks\)", result.stdout)
        self.assertTrue(claimed, result.stdout[-300:])
        documented = re.search(r"فحص عقد المكوّنات:\s*(\d+)", PLAN)
        self.assertTrue(documented, "the plan must state the components contract count")
        self.assertEqual(int(documented.group(1)), int(claimed.group(1)),
                         "the harness changed; the doc number has to move with it")


if __name__ == "__main__":
    unittest.main()
