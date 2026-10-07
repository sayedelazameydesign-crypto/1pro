"""Assert behaviour, not sentences.

Twice in one session a green suite went red on wording rather than on behaviour: a
test asserted `assertIn("رفض", ...)` against a message `tools.py` writes, and a
release-note patch asserted on a sentence containing an em dash. Neither failure
meant anything was broken. Both taught the same lesson -- a suite that breaks when
the Arabic improves teaches people to weaken tests, and the weakened version is the
one that later misses a real regression.

The fix that makes this checkable is a machine-readable half for every refusal:
`SandboxRefused.code`, `ToolError.code`, `status` values, capability constants,
`error_code` on a failed task. With those in place, a test never *needs* to assert
on a sentence, so this file can refuse the pattern instead of merely discouraging it.

The rule, kept narrow enough to be trusted:

    No assertion may match a *substring* of a string the R7 surface can emit, unless
    what it matches is an identifier.

    * Identifiers -- `filesystem_isolation`, `recorded_at`, `pre_http_network_failure`,
      `[read-only]`, `WAHA_TRUSTED_HOSTS` -- are the structural anchors we want, so
      single Latin tokens pass: rewording the prose around them cannot change them.
    * Arabic is copy by default, at any length. This is the case that started it:
      `assertIn("عزل", ...)` against a refusal message. So a single Arabic word is
      flagged, not only a sentence, and the fix is to assert the code.
    * Strings that appear only in a docstring or comment are ignored: prose *about*
      the code is not something a program can emit.

Where a match is *content the test itself supplied* -- a question, a goal, a step
title -- the answer is not an exception but better data: compare the whole value, or
use a distinctive title that cannot coincide with the tool copy.

Scope is the R7 surface on purpose -- the module that refuses, the registry that
describes tools, and the loop that records their failures. `docs/assets/app.js`
labels and CLI banners are their own contracts, tested where they live; widening
this guard means first giving that surface a machine-readable half, which is the
order this repository prefers anyway.

The tests for the *content* the agent handles (a user's question about "بايثون", a
retrieved snippet) are untouched by design: those literals come from the test or the
corpus, not from a message the implementation wrote to be read.
"""
import ast
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

# The modules whose user-facing copy is a *decision* the suite must be able to check
# without reading the copy.
SURFACE = ("backend/agent/sandbox.py", "backend/agent/tools.py", "backend/agent/runtime.py")

ARABIC = re.compile(r"[\u0600-\u06FF]")
ASSERTIONS = ("assertIn", "assertNotIn", "assertRegex")

# Every entry is a deliberate exception, and each one has to say why it is not copy.
# The list is capped below: if a fourth ever looks necessary, the right answer is a
# machine-readable half for that surface, not another line here.
ALLOWED = {
    ("test_agent_runtime.py", "assertIn", "event: close"):
        "SSE is a wire format: `event: close` is protocol syntax, and changing it "
        "would break clients rather than describe the change in a nicer sentence",
}


def docstring_nodes(tree):
    """Every docstring literal in a module: prose about code, not output of it."""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                found.add(id(body[0].value))
    return found


def emittable_strings():
    """Every string literal the surface can actually emit, with where it lives."""
    strings = []
    for relative in SURFACE:
        source = (ROOT / relative).read_text(encoding="utf-8")
        tree = ast.parse(source)
        skip = docstring_nodes(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and id(node) not in skip and len(node.value) >= 3:
                strings.append((node.value, relative, node.lineno))
    return strings


def suspect_assertions():
    """Assertions that quote the implementation's copy instead of its decisions."""
    emitted = emittable_strings()
    suspects = []
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr not in ASSERTIONS:
                continue
            for argument in node.args:
                if not (isinstance(argument, ast.Constant)
                        and isinstance(argument.value, str) and len(argument.value) >= 3):
                    continue
                literal = argument.value
                if (path.name, node.func.attr, literal) in ALLOWED:
                    continue
                # Latin identifiers pass; Arabic is copy at any length; a phrase in any
                # script is a sentence. Whitespace-only strings carry no words at all.
                if not (ARABIC.search(literal) or re.search(r"\w\s+\w", literal)):
                    continue
                for sentence, where, line in emitted:
                    if literal in sentence and literal != sentence:
                        suspects.append((path.name, node.lineno, node.func.attr, literal, where, line))
                        break
    return suspects


class AssertionsSurviveAnEdit(unittest.TestCase):
    """The guard. A failure here is not "you may not test the message" -- it is
    "give the decision a code and assert that instead", which is the change that
    makes the assertion survive its own copy."""

    def test_no_assertion_quotes_the_r7_implementation_copy(self):
        suspects = suspect_assertions()
        if suspects:
            lines = ["an assertion matches a substring of a string the implementation emits:",
                     "assert the decision (code / status / capability / field) instead of the copy:", ""]
            for name, lineno, call, literal, where, line in suspects:
                lines.append(f"  {name}:{lineno}  {call}({literal!r})")
                lines.append(f"      matches {where}:{line}")
            self.fail("\n".join(lines))

    def test_the_guard_itself_would_catch_the_pattern(self):
        """A guard nobody has falsified is a guard nobody should trust. This plants
        the exact shape that broke the suite -- an Arabic fragment of a message -- in
        a temporary test file and requires the scan to report it."""
        planted = ROOT / "tests" / "test_zz_planted_copy.py"
        try:
            # Both shapes that actually failed: the single Arabic word asserted
            # against a refusal message, and a phrase asserted against another one.
            planted.write_text(
                "import unittest\n\n\nclass Planted(unittest.TestCase):\n"
                "    def test_single_word(self):\n"
                "        self.assertIn('عزل', 'refused inside the boundary')\n\n"
                "    def test_phrase(self):\n"
                "        self.assertIn('غير مدعومة في الآلة الحاسبة', 'some output')\n",
                encoding="utf-8")
            reported = [row for row in suspect_assertions() if row[0] == planted.name]
        finally:
            planted.unlink(missing_ok=True)
        self.assertEqual(sorted(row[3] for row in reported), ["عزل", "غير مدعومة في الآلة الحاسبة"],
                         "the scan missed a planted assertion on implementation copy")
        self.assertTrue(all(row[2] == "assertIn" and row[4] == "backend/agent/tools.py"
                            for row in reported))

    def test_the_allowlist_cannot_grow_silently(self):
        """Exceptions are how a guard becomes decorative. Each one must carry a
        reason, and there is a ceiling: past it, the surface needs a code."""
        for key, reason in ALLOWED.items():
            with self.subTest(entry=key):
                self.assertTrue(reason.strip(), f"{key} is allowed without a reason")
                self.assertGreater(len(reason), 40, f"{key}'s reason is too thin to review")
        self.assertLessEqual(len(ALLOWED), 3,
                             "a fourth exception means the surface needs a machine-readable "
                             "half, not another allowlist line")

    def test_identifiers_and_test_supplied_text_are_not_flagged(self):
        """The rule has to allow what it should allow, or it will be switched off:
        a capability constant is fine, and so is content the test authored itself."""
        planted = ROOT / "tests" / "test_zz_planted_ok.py"
        try:
            planted.write_text(
                "import unittest\n\n\nclass Planted(unittest.TestCase):\n"
                "    def test_planted(self):\n"
                "        self.assertIn('filesystem_isolation', 'refused: filesystem_isolation')\n"
                "        self.assertIn('سؤال من المستخدم', 'سؤال من المستخدم')\n",
                encoding="utf-8")
            reported = [row for row in suspect_assertions() if row[0] == planted.name]
        finally:
            planted.unlink(missing_ok=True)
        self.assertEqual(reported, [], "the guard flagged a structural assertion")


if __name__ == "__main__":
    unittest.main()
