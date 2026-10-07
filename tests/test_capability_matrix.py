"""The capability matrix must describe the code that exists, not the code we meant.

`CAPABILITY-MATRIX.md` grades 89 capabilities against Claude and Manus. A matrix
that drifts is worse than no matrix: it is the document a roadmap gets built on,
and a stale `Implemented` is a promise the code does not keep. This file is the
same rule the rest of the suite already applies to numbers -- a documented claim
is asserted against its source.

Every row must carry evidence a machine can resolve, so "we have a browser agent"
cannot be written until `tool:browser_navigate` actually resolves. The inverse is
enforced too: `Missing` rows must cite nothing, so a capability cannot be closed
by prose.

This file does not judge the priorities. It checks that a claim resolves, that an
absence is admitted as one, and that the published totals are the rows.
"""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from agent.config import AgentConfig  # noqa: E402
from agent.tools import build_registry  # noqa: E402

MATRIX = (ROOT / "CAPABILITY-MATRIX.md").read_text(encoding="utf-8")
APP = (ROOT / "backend" / "app.py").read_text(encoding="utf-8")
STORE = (ROOT / "backend" / "agent" / "store.py").read_text(encoding="utf-8")

STATUSES = ("Implemented", "Partial", "Missing", "Mocked")
PRIORITIES = ("P0", "P1", "P2")
# A row is: id | capability | C | M | status | evidence | priority. Anchored on the
# id, so the prose tables above and below the matrix are not mistaken for rows.
ROW = re.compile(r"^\|\s*(C\d{2})\s*\|(.+?)\|(.+?)\|(.+?)\|"
                 r"\s*(Implemented|Partial|Missing|Mocked)\s*\|(.+?)\|\s*(P0|P1|P2)\s*\|\s*$",
                 re.M)
TOKEN = re.compile(r"`([a-z]+):([^`]+)`")
# An evidence cell that cites something, whatever it is rendered with: a Missing row
# must match none of these tags, while how a row says "nothing" is a formatting choice.
EVIDENCE_TAG = re.compile(r"\b(?:file|tool|route|table|knob):")
TOTAL = re.compile(r"الإجمالي:\s*(\d+)\s*قدرة\s*—(.+)")


class MatrixIndex:
    """The code side of every evidence tag, read once."""

    def __init__(self):
        self.tools = set(build_registry().names())
        self.routes = set(re.findall(r'@app\.(?:get|post|route)\(\s*"([^"]+)"', APP))
        self.tables = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", APP + STORE))
        self.module_knobs = set(re.findall(r"^([A-Z][A-Z0-9_]+)\s*=", APP, re.M))

    def resolves(self, kind, value):
        if kind == "file":
            return (ROOT / value).exists()
        if kind == "tool":
            return value in self.tools
        if kind == "route":
            return value in self.routes
        if kind == "table":
            return value in self.tables
        if kind == "knob":
            return hasattr(AgentConfig, value) or value in self.module_knobs
        return False


INDEX = MatrixIndex()


def rows():
    out = []
    for match in ROW.finditer(MATRIX):
        identifier, name, c, m, status, evidence, priority = match.groups()
        out.append({"id": identifier, "name": name.strip(), "claude": c.strip(),
                    "manus": m.strip(), "status": status, "evidence": evidence.strip(),
                    "priority": priority,
                    "tokens": TOKEN.findall(evidence)})
    return out


class TheMatrixIsWellFormed(unittest.TestCase):
    def test_it_grade_every_row_on_the_declared_vocabulary(self):
        parsed = rows()
        self.assertGreaterEqual(len(parsed), 50,
                                "the matrix is meant to cover 50-100 capabilities")
        for row in parsed:
            with self.subTest(row=row["id"]):
                self.assertIn(row["claude"], ("✅", "◐", "❌"),
                              "a Claude cell outside the legend")
                self.assertIn(row["manus"], ("✅", "◐", "❌"),
                              "a Manus cell outside the legend")
                self.assertTrue(row["name"], "a capability with no name")
        # The regex only matches the four statuses and three priorities, so an
        # out-of-vocabulary row silently disappears. Count the raw table lines and
        # fail loudly instead of shrinking the matrix into a passing run.
        raw = [line for line in MATRIX.splitlines() if re.match(r"^\|\s*C\d{2}\s*\|", line)]
        self.assertEqual(len(raw), len(parsed),
                         "a row uses a status or priority outside the vocabulary")
        for status in STATUSES:
            self.assertIn(status, MATRIX, f"the legend lost `{status}`")

    def test_the_ids_are_unique_and_contiguous(self):
        identifiers = [row["id"] for row in rows()]
        self.assertEqual(len(identifiers), len(set(identifiers)), "a duplicated row id")
        self.assertEqual(identifiers, [f"C{index:02d}" for index in range(1, len(identifiers) + 1)],
                         "the ids are no longer C01..Cn with no gaps")


class EvidenceResolves(unittest.TestCase):
    def test_every_claim_resolves_against_the_code(self):
        for row in rows():
            if row["status"] == "Missing":
                continue
            with self.subTest(row=row["id"], capability=row["name"][:40]):
                self.assertTrue(row["tokens"],
                                f"{row['id']} is {row['status']} with no evidence: a claim "
                                "must cite something a machine can check")
                for kind, value in row["tokens"]:
                    self.assertIn(kind, ("file", "tool", "route", "table", "knob"),
                                  f"{row['id']} uses an unknown evidence tag `{kind}:`")
                    self.assertTrue(INDEX.resolves(kind, value),
                                    f"{row['id']} cites `{kind}:{value}`, which does not "
                                    "resolve -- the matrix has drifted from the code")

    def test_a_missing_capability_admits_it_cites_nothing(self):
        for row in rows():
            if row["status"] != "Missing":
                continue
            with self.subTest(row=row["id"], capability=row["name"][:40]):
                self.assertFalse(row["tokens"],
                                 f"{row['id']} is Missing yet cites evidence; absence is not "
                                 "provable from a path -- mark it Partial and cite what exists")
                # What matters is that the cell cites nothing, so that is what is
                # asserted. The previous version matched the dash a Missing row is
                # rendered with -- punctuation, not behaviour: it would have broken on
                # a formatting change that meant nothing, on the same principle that
                # keeps this suite from asserting on sentences.
                self.assertFalse(EVIDENCE_TAG.search(row["evidence"]),
                                 f"{row['id']} is Missing yet its evidence cell cites "
                                 f"{row['evidence']!r}")


class TheMatrixCoversTheTools(unittest.TestCase):
    def test_every_registered_tool_is_accounted_for(self):
        """A tool nobody documented is a capability the matrix claims to audit and
        does not. This is the same rule the plan's test-count table enforces."""
        documented = {value for row in rows() for kind, value in row["tokens"]
                      if kind == "tool"}
        undocumented = INDEX.tools - documented
        self.assertFalse(undocumented,
                         f"registered tools missing from the matrix: {sorted(undocumented)}")
        stale = documented - INDEX.tools
        self.assertFalse(stale, f"the matrix documents tools that no longer exist: {sorted(stale)}")


class ThePublishedTotalsAreTheRows(unittest.TestCase):
    def test_the_total_line_matches_the_table(self):
        match = TOTAL.search(MATRIX)
        self.assertTrue(match, "the matrix lost its totals line")
        self.assertEqual(int(match.group(1)), len(rows()),
                         "the stated capability count is not the number of rows")
        counts = {status: 0 for status in STATUSES}
        for row in rows():
            counts[row["status"]] += 1
        for status, count in counts.items():
            with self.subTest(status=status):
                self.assertIn(f"{status} {count}", match.group(2),
                              f"the totals line does not state {status}={count}")

    def test_the_readme_points_at_the_matrix(self):
        """A matrix nobody is told about is a file, not a contract."""
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("CAPABILITY-MATRIX.md", readme,
                      "README must link the matrix next to the rest of the docs")


if __name__ == "__main__":
    unittest.main()
