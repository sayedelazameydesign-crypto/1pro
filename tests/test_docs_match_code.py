"""The published contract must match the measured code.

Every number a reader would quote from the docs is asserted here against the value the
code actually uses, plus the test counts are generated from the files themselves. The
trigger was concrete: the README described `starter` at 0.85 while `SECTION_WEIGHTS` had
shipped 0.55 since R2, and the PR description kept advertising a suite size two phases
old. Both were invisible to every test that only reads the code.

This file does not judge prose or style. It checks three things and nothing else:
a documented number equals its source, a documented total equals the sum of its parts,
and no test file is uncounted.
"""
import ast
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import rag_search  # noqa: E402
from agent import tools as agent_tools  # noqa: E402
from agent.config import AgentConfig  # noqa: E402

README = (ROOT / "README.md").read_text(encoding="utf-8")
PLAN = (ROOT / "RAG-FREE-PLAN.md").read_text(encoding="utf-8")
ARCH = (ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")
DOCS = {"README.md": README, "RAG-FREE-PLAN.md": PLAN, "ARCHITECTURE.md": ARCH}


# The R2 sentence, in both files. A window rather than a line, because wrapped Arabic prose
# puts the numbers a couple of visual lines below the phrase that introduces them.
WEIGHT_SENTENCE = {"README.md": "BM25 فوق `postings` المودَعة",
                   "RAG-FREE-PLAN.md": "وزن لكل قسم داخل تراكم الدرجة"}


def python_test_count(path):
    """Count `def test_*` by parsing, so a mention in prose cannot fake a test."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return sum(1 for node in ast.walk(tree)
               if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"))


class RetrievalWeightsArePublished(unittest.TestCase):
    def test_section_weights_appear_with_their_real_values(self):
        for name, text in WEIGHT_SENTENCE.items():
            index = DOCS[name].find(text)
            self.assertGreater(index, -1, f"{name} lost its R2 weight sentence")
            window = DOCS[name][index:index + 300]
            for section, weight in sorted(rag_search.SECTION_WEIGHTS.items()):
                self.assertIn(str(weight), window,
                              f"{name} describes `{section}` without its shipped weight "
                              f"{weight} (window: {window[:140]}...)")

    def test_the_superseded_weight_is_not_still_in_the_docs(self):
        # 0.85 was the number from the plan before measurement; it kept living in prose.
        for name, text in DOCS.items():
            self.assertNotIn("0.85", text, f"{name} still advertises starter=0.85")

    def test_default_section_weight_is_the_documented_one(self):
        # An unknown section falls back to 1.0; if the docs only name the two interesting
        # weights, a reader cannot tell what an unlabelled chunk would score.
        self.assertEqual(rag_search.DEFAULT_SECTION_WEIGHT, rag_search.SECTION_WEIGHTS["description"])
        self.assertIn(str(rag_search.DEFAULT_SECTION_WEIGHT), README + PLAN)


class AgentLimitsArePublished(unittest.TestCase):
    def test_kb_budgets_match_the_registry_constants(self):
        for name, value in (("MAX_KB_RESULTS", agent_tools.MAX_KB_RESULTS),
                            ("MAX_KB_SNIPPET_CHARS", agent_tools.MAX_KB_SNIPPET_CHARS),
                            ("MAX_KB_CONTEXT_CHARS", agent_tools.MAX_KB_CONTEXT_CHARS)):
            self.assertIn(f"{name}={value}", README + PLAN, f"{name} is not documented as {value}")
        self.assertLess(agent_tools.MAX_KB_RESULTS, rag_search.MAX_RESULTS,
                        "the agent's ceiling must stay below the endpoint's")

    def test_no_second_query_ceiling_exists(self):
        code = ast.unparse(ast.parse((ROOT / "backend" / "agent" / "tools.py").read_text(encoding="utf-8")))
        self.assertNotIn("MAX_QUERY_CHARS =", code,
                         "tools.py redefining the query ceiling is the drift this file exists to catch")

    def test_advertised_limits_are_the_enforced_ones(self):
        described = AgentConfig.describe()
        for key in ("max_steps", "max_ai_calls", "deadline_seconds"):
            self.assertIn(key, described)
        # R6: the mode is advertised next to the budget it enforces, not instead of it.
        app = (ROOT / "backend" / "app.py").read_text(encoding="utf-8")
        self.assertIn("execution=policy.describe()", app)
        self.assertIn("limits=AgentConfig.describe()", app)

    def test_inline_caps_are_documented_where_they_are_used(self):
        from agent import execution
        self.assertIn("SERVERLESS_CAPS", README + PLAN)
        self.assertEqual(execution.SERVERLESS_CAPS["DEADLINE_SECONDS"], 45)
        self.assertIn("45", README)


class GeneratedTestCounts(unittest.TestCase):
    """The plan's table is generated from the files; nothing may be uncounted."""

    def setUp(self):
        match = re.search(r"\| الملف \| `def test_` \|", PLAN)
        self.assertTrue(match, "the plan lost its test-count table")
        tail = PLAN[match.start():]
        self.rows = {}
        for line in tail.splitlines():
            found = re.match(r"^\|\s*`?(?:\*\*)?(tests/[\w.]+\.py)(?:\*\*)?`?\s*\|\s*(\d+)\s*\|", line)
            if found:
                self.rows[found.group(1)] = int(found.group(2))
        total = re.search(r"\|\s*\*\*المجموع\*\*\s*\|\s*\*\*(\d+)\*\*\s*\|", tail)
        self.assertTrue(total, "the table lost its totals row")
        self.total = int(total.group(1))

    def test_table_matches_the_files_exactly(self):
        actual = {f"tests/{path.name}": python_test_count(path)
                  for path in sorted((ROOT / "tests").glob("test_*.py"))}
        self.assertEqual(self.rows, actual,
                         "the documented counts moved away from the files -- update the table "
                         "in the same commit that changes a test, not after")

    def test_table_is_complete_and_adds_up(self):
        self.assertEqual(sum(self.rows.values()), self.total)
        on_disk = {f"tests/{path.name}" for path in (ROOT / "tests").glob("test_*.py")}
        self.assertEqual(set(self.rows), on_disk,
                         "a test file outside the table is how a suite size goes stale")

    def test_readme_quotes_the_same_total_as_the_table(self):
        # The PR description rotted by advertising a two-phases-old suite size; the README is
        # the same kind of claim, so it is checked against the generated table, not against
        # anyone's memory. Only "N اختبار" phrasings are considered.
        claimed = {int(n) for n in re.findall(r"(\d{2,4})\s+اختبار", README)}
        self.assertIn(self.total, claimed,
                      f"README never states the suite total {self.total}; its numbers are: {sorted(claimed)}")

    def test_the_browser_harness_reports_the_documented_number(self):
        if shutil.which("node") is None:
            self.skipTest("node is not installed")
        result = subprocess.run(["node", str(ROOT / "tests" / "browser_search.test.mjs")],
                                capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr[-400:])
        claimed = re.search(r"browser search contract: ok \((\d+) checks\)", result.stdout)
        self.assertTrue(claimed, result.stdout[-300:])
        documented = re.search(r"فحص عقد المتصفح:\s*(\d+)", PLAN)
        self.assertTrue(documented, "the plan must state the browser contract count")
        self.assertEqual(int(documented.group(1)), int(claimed.group(1)),
                         "the harness changed; the doc number has to move with it")


if __name__ == "__main__":
    unittest.main()
