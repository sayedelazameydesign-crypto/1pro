"""R4: the eval set, the committed report, and the gate that reads it.

These tests read the artifacts as they ship -- no rebuild, no fixture directory -- so a
committed report that disagrees with the code that produced it fails CI loudly. The
metric math itself is covered by `scripts/rag_eval.py --self-test`; this file covers the
part that only the repo can prove: the numbers published here are the numbers the
retriever produces, and the vector gate is reading them.
"""
import hashlib
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "backend"))

import rag_eval  # noqa: E402
import rag_index  # noqa: E402
import rag_search  # noqa: E402

DATA = ROOT / "data" / "rag"
REPORT = DATA / "eval-report.json"
TRAIN = ROOT / "eval" / "train.json"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


class CommittedReport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = read(REPORT)
        cls.train = read(TRAIN)

    def test_report_matches_a_fresh_run_byte_for_byte(self):
        problems, fresh = rag_eval.check(out_dir=DATA, quiet=True)
        self.assertEqual(problems, [], "\n".join(problems))
        self.assertEqual(rag_eval.report_bytes(fresh), REPORT.read_bytes())
        self.assertEqual(fresh["metrics"], self.report["metrics"])

    def test_the_agreed_gate_passes_on_the_committed_number(self):
        metrics = self.report["metrics"]
        ceiling = rag_index.GATE_THRESHOLDS["lexical_recall_at5"]
        self.assertEqual(ceiling, 0.80)
        self.assertGreaterEqual(metrics["recall_at5"], ceiling)
        self.assertEqual(metrics["false_answer_rate"], 0.0,
                         "an out-of-catalog question must not return a claimed hit")
        self.assertEqual(metrics["no_answer_rate"], 1.0)
        self.assertEqual(metrics["citation_correctness"], 1.0)
        self.assertEqual(metrics["duplicate_hit_rate"], 0.0)
        for key, value in metrics.items():
            self.assertIsInstance(value, float, key)
            self.assertGreaterEqual(value, 0.0, key)
            self.assertLessEqual(value, 1.0 if key != "mean_results" else 99.0, key)

    def test_stats_json_gate_reads_this_report(self):
        """The coupling that makes R5 a measured decision instead of a conversation."""
        stats = read(DATA / "stats.json")
        gate = stats["vector_gate"]
        self.assertEqual(gate["measured"]["lexical_recall_at5"], self.report["metrics"]["recall_at5"])
        self.assertFalse(gate["vector_enabled"],
                         "recall@5 is above the ceiling on a 6-skill sample: vectors stay off")
        self.assertEqual(gate["decision"], "lexical-only")
        self.assertEqual(gate["reasons"], [])
        self.assertEqual(gate["thresholds"]["lexical_recall_at5"], 0.80)

    def test_report_points_at_the_index_it_scored(self):
        sha = "sha256:" + hashlib.sha256((DATA / "index.json").read_bytes()).hexdigest()
        self.assertEqual(self.report["index_sha256"], sha,
                         "a report must be detectably stale rather than merely probably fresh")
        self.assertEqual(self.report["corpus"]["format"], rag_index.FORMAT)
        self.assertEqual(self.report["corpus"]["chunks"], 18)
        self.assertEqual(self.report["corpus"]["content_status"], "sample")
        self.assertEqual(self.report["format"], rag_eval.EVAL_FORMAT)
        self.assertEqual(self.report["search_version"], rag_search.SEARCH_VERSION)

    def test_no_wall_clock_in_a_deterministic_artifact(self):
        text = REPORT.read_text(encoding="utf-8")
        for forbidden in ("generated_at", "timestamp", "\"now\"", "built_at"):
            self.assertNotIn(forbidden, text)

    def test_check_writes_nothing(self):
        before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in sorted(DATA.iterdir()) if path.is_file()}
        subprocess.run([sys.executable, str(ROOT / "scripts/rag_eval.py"), "--check"],
                       cwd=ROOT, capture_output=True, text=True, check=True)
        after = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in sorted(DATA.iterdir()) if path.is_file()}
        self.assertEqual(before, after)


class EvalSet(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.train = read(TRAIN)
        cls.report = read(REPORT)
        cls.corpus = [json.loads(line) for line in
                      (DATA / "corpus.jsonl").read_text(encoding="utf-8").splitlines()]
        cls.citations = {row["citation"] for row in cls.corpus}

    def test_every_case_is_measured_and_reported_in_order(self):
        self.assertEqual([case["id"] for case in self.train["cases"]],
                         [row["id"] for row in self.report["cases"]],
                         "a case silently dropped from the report would inflate the metric")

    def test_ground_truth_citations_exist_in_r1(self):
        for case in self.train["cases"]:
            for citation in (case["ground_truth"] or []):
                self.assertIn(citation, self.citations, case["id"])

    def test_the_set_keeps_the_hard_cases_it_would_like_to_drop(self):
        by_id = {case["id"]: case for case in self.train["cases"]}
        self.assertEqual(by_id["q05"]["query"], "بايثون",
                         "the transliteration gap stays a scored case, not a footnote")
        self.assertEqual(by_id["q16"]["query"], "إدارة الوقت",
                         "the skill-name-only gap is measured too")
        self.assertIsNone(by_id["n01"]["ground_truth"])
        self.assertIn("الكبة", by_id["n01"]["query"], "the near-miss brake test is required")
        self.assertIsNone(by_id["n02"]["ground_truth"])
        self.assertIn("اليوم", by_id["n02"]["query"],
                      "the exact query that over-stemming turned into a false hit")

    def test_advisory_cases_are_reported_but_not_gated(self):
        gated = self.report["metrics"]["no_answer_rate"]
        advisory = [row for row in self.report["cases"] if row["advisory"]]
        self.assertTrue(advisory, "the eval set must keep at least one honest noisemaker")
        self.assertTrue(any(row["returned"] for row in advisory),
                        "if the advisory case ever passes, delete the flag -- it stops earning it")
        negatives = [row for row in self.report["cases"]
                     if row["kind"] == "no_answer" and not row["advisory"]]
        self.assertEqual(gated, round(sum(row["recall_at5"] for row in negatives) / len(negatives), 4))
        self.assertEqual(self.report["counts"]["advisory_cases"], len(advisory))

    def test_hard_misses_are_visible_in_the_report(self):
        misses = {row["id"] for row in self.report["cases"] if row["recall_at5"] == 0
                  and not row["advisory"]}
        self.assertEqual(misses, {"q05"},
                         "the known gaps are listed by id so a fix can be verified")


class ScriptInterface(unittest.TestCase):
    def test_self_test_passes_and_says_what_it_covered(self):
        result = subprocess.run([sys.executable, str(ROOT / "scripts/rag_eval.py"), "--self-test"],
                                cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr[-600:])
        self.assertIn("rag_eval self-test: ok", result.stdout)
        for word in ("recall", "precision", "false-answer", "citation"):
            self.assertIn(word, result.stdout)

    def test_a_stale_or_tampered_report_is_refused(self):
        # Copy the whole index dir, corrupt only the report, and confirm --check objects.
        import shutil
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("index.json", "corpus.jsonl", "manifest.json", "stats.json"):
                shutil.copy(DATA / name, Path(tmp) / name)
            (Path(tmp) / "eval-report.json").write_text('{"metrics": {"recall_at5": 1.0}}\n',
                                                         encoding="utf-8")
            problems, _ = rag_eval.check(out_dir=Path(tmp), quiet=True)
            self.assertTrue(any("stale" in item for item in problems), problems)

    def test_missing_report_is_named_as_missing(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("index.json", "corpus.jsonl"):
                (Path(tmp) / name).write_bytes((DATA / name).read_bytes())
            problems, _ = rag_eval.check(out_dir=Path(tmp), quiet=True)
            self.assertTrue(any("is missing" in item for item in problems), problems)


if __name__ == "__main__":
    unittest.main()
