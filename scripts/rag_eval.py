"""R4: measure the retriever, and let that number decide everything downstream.

Why this file exists at all: until retrieval is scored, "the search works" and "the
search looks fine on three questions I liked" are the same sentence. This stage turns
that into a number CI can fail on, and it is the ONLY writer of
`data/rag/eval-report.json` -- the file R1's vector gate reads. A hand-run probe must
never produce a gate input, so `--check` compares bytes instead of trusting any report
that happens to be lying around.

Design choices worth defending:

* **Questions are drawn from what the corpus actually contains.** With six skills and 18
  chunks, a question about a topic nobody wrote about measures the catalog, not the
  ranker. Cases that SHOULD fail are kept anyway (`q05` transliteration, `q16`
  skill-name-only) -- removing hard cases to raise a score is how a metric becomes
  decoration.
* **`ground_truth: null` means "return nothing".** That is scored as
  `no_answer_rate`, and a hit on such a case is a `false_answer`: the shape of failure
  that turns into a confident invented citation once a model starts reading these rows.
* **No thresholds invented here either** -- except the one the project already agreed
  on, imported from `rag_index.GATE_THRESHOLDS["lexical_recall_at5"]`. The eval reuses
  that ceiling rather than minting its own, so there is still exactly one number in the
  repo that decides whether vectors are justified.
* **Deterministic bytes.** No timestamps, no counters, no dict ordering surprises:
  case order is file order, metrics are rounded, and the corpus identity is recorded as
  the index sha256 so a stale report is detectable rather than merely likely.

Usage:
    python scripts/rag_eval.py            # run and write data/rag/eval-report.json
    python scripts/rag_eval.py --check    # CI: recompute, compare bytes, enforce the gate
    python scripts/rag_eval.py --self-test
"""
import argparse
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from build_catalog import ROOT, json_bytes  # noqa: E402
import rag_index  # noqa: E402  (gate thresholds + format constants: imported, never copied)
import rag_search  # noqa: E402  (the retrieval under test -- the same one the agent uses)

EVAL_FORMAT = "waha.rag-eval.v1"
EVAL_VERSION = "waha.rag-eval/1"
RECALL_AT = 5
TRAIN_PATH = ROOT / "eval/train.json"
REPORT_NAME = "eval-report.json"
METRIC_KEYS = ("recall_at5", "precision_at_1", "no_answer_rate", "false_answer_rate",
               "citation_correctness", "duplicate_hit_rate", "mean_results")


class EvalError(RuntimeError):
    """The evaluation set or its report is unusable -- refuse to produce a number."""


def load_cases(path=TRAIN_PATH):
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise EvalError(f"cannot read {path}: {error}") from error
    if document.get("format") != EVAL_FORMAT:
        raise EvalError(f"{path}: format must be {EVAL_FORMAT!r}")
    cases = document.get("cases")
    if not isinstance(cases, list) or not cases:
        raise EvalError(f"{path}: no cases")
    seen = set()
    for case in cases:
        for key in ("id", "kind", "query", "ground_truth"):
            if key not in case:
                raise EvalError(f"case {case.get('id')!r} is missing {key!r}")
        if case["id"] in seen:
            raise EvalError(f"duplicate case id {case['id']!r}")
        seen.add(case["id"])
        if case["kind"] not in ("answerable", "no_answer"):
            raise EvalError(f"case {case['id']}: kind must be answerable or no_answer")
        if case["kind"] == "answerable" and not case["ground_truth"]:
            raise EvalError(f"case {case['id']}: answerable needs at least one citation")
        if case["kind"] == "no_answer" and case["ground_truth"] is not None:
            raise EvalError(f"case {case['id']}: no_answer needs ground_truth: null")
    return cases


def score_case(case, payload, corpus_pairs):
    """One case -> a row of the report. Pure, so --self-test can feed it anything."""
    results = payload.get("results") or []
    citations = [row["citation"] for row in results]
    chunk_ids = [row["chunk_id"] for row in results]
    truth = case.get("ground_truth")
    row = {
        "id": case["id"],
        "kind": case["kind"],
        "query": case["query"],
        "ground_truth": truth,
        "returned": citations,
        "scores": [row["score"] for row in results],
        "coverage": (payload.get("query_stats") or {}).get("coverage"),
        "inferred_only": bool(results) and all(row["explain"]["matched_weight"] != 1.0
                                               for row in results),
        "no_answer_decision": (payload.get("no_answer") or {}).get("decision"),
        "duplicates": len(chunk_ids) - len(set(chunk_ids)),
        # An advisory case is measured and published but kept out of the gated
        # aggregates. The honest response to an awkward finding is to report it, not to
        # delete the question that produced it.
        "advisory": bool(case.get("advisory")),
        # Every returned row must be a real (citation, chunk_id) pair from R1's corpus.
        # A number that cannot be traced back is not evidence, however well it ranks.
        "citation_correct": all((item[0], item[1]) in corpus_pairs
                               for item in zip(citations, chunk_ids)),
    }
    if case["kind"] == "no_answer":
        row["expected"] = []
        row["recall_at5"] = 1 if not citations else 0
        row["precision_at_1"] = 1 if not citations else 0
        row["false_answer"] = bool(citations)
    else:
        accepted = set(truth)
        row["expected"] = list(truth)
        row["recall_at5"] = 1 if accepted & set(citations[:RECALL_AT]) else 0
        row["precision_at_1"] = 1 if citations and citations[0] in accepted else 0
        row["false_answer"] = 0
    return row


def evaluate(cases, index_dir=None):
    """Run every case through R2's own search and roll the numbers up."""
    state = rag_search.load(index_dir)
    corpus_pairs = {(row["citation"], row["chunk_id"]) for row in state["rows"].values()}
    wanted = {citation for case in cases if case["kind"] == "answerable"
              for citation in case["ground_truth"]}
    unknown = sorted(citation for citation in wanted
                     if citation not in {pair[0] for pair in corpus_pairs})
    if unknown:
        # A citation that no longer exists means the eval set drifted from the corpus.
        # Scoring it as a miss would blame the retriever for our own stale fixture.
        raise EvalError("ground_truth citations absent from the corpus: " + ", ".join(unknown))

    rows = []
    for case in cases:
        payload = rag_search.search(case["query"], k=RECALL_AT, index_dir=index_dir)
        rows.append(score_case(case, payload, corpus_pairs))

    gated = [row for row in rows if not row["advisory"]]
    answerable = [row for row in gated if row["kind"] == "answerable"]
    negatives = [row for row in gated if row["kind"] == "no_answer"]
    if not answerable or not negatives:
        raise EvalError("the eval set needs at least one answerable and one no_answer case")

    def rate(rows_, key):
        return round(sum(row[key] for row in rows_) / len(rows_), 4) if rows_ else 0.0

    returned_rows = sum(len(row["returned"]) for row in rows)
    metrics = {
        "recall_at5": rate(answerable, "recall_at5"),
        "precision_at_1": rate(answerable, "precision_at_1"),
        # Of the questions that must NOT be answered, how many weren't.
        "no_answer_rate": rate(negatives, "recall_at5"),
        "false_answer_rate": round(sum(1 for row in negatives if row["false_answer"]) /
                                   len(negatives), 4) if negatives else 0.0,
        "citation_correctness": 1.0 if all(row["citation_correct"] for row in rows) else 0.0,
        "duplicate_hit_rate": (round(sum(1 for row in gated if row["duplicates"]) / len(gated), 4)
                               if gated else 0.0),
        "mean_results": round(returned_rows / len(rows), 4) if rows else 0.0,
    }
    status = {
        "cases": len(rows),
        "answerable": len(answerable),
        "no_answer": len(negatives),
        "zero_hit_cases": sum(1 for row in rows if not row["returned"]),
        "inferred_only_cases": sum(1 for row in rows if row["inferred_only"]),
        "advisory_cases": len(rows) - len(gated),
    }
    return metrics, status, rows


def build_report(index_dir=None, cases_path=TRAIN_PATH):
    cases = load_cases(cases_path)
    metrics, status, rows = evaluate(cases, index_dir=index_dir)
    state = rag_search.load(index_dir)
    index = state["index"]
    index_path = Path(state["directory"]) / "index.json"
    index_sha = "sha256:" + hashlib.sha256(index_path.read_bytes()).hexdigest()
    return {
        "format": EVAL_FORMAT,
        "eval_version": EVAL_VERSION,
        "recall_at": RECALL_AT,
        # Which index these numbers belong to. A report whose sha no longer matches is
        # stale by construction, without needing a timestamp to guess it.
        "index_sha256": index_sha,
        "corpus": {
            "format": index["format"],
            "pipeline": index["pipeline"],
            "tokenizer": index["pipeline"].get("tokenizer"),
            "chunks": index["chunk_count"],
            "indexed_chunks": len(state["rows"]),
            "docs": index["doc_count"],
            "vocab": index["vocab"],
            "content_status": index.get("content_status"),
        },
        "search_version": rag_search.SEARCH_VERSION,
        "params": {"k": RECALL_AT, "bm25_k1": rag_search.BM25_K1, "bm25_b": rag_search.BM25_B},
        "gate": {"lexical_recall_at5": rag_index.GATE_THRESHOLDS["lexical_recall_at5"],
                 "policy": "recall_at5 below the ceiling is what justifies vectors"},
        "counts": status,
        "metrics": metrics,
        "cases": rows,
    }


def report_bytes(report):
    return json_bytes(report)


def check(index_dir=None, out_dir=None, quiet=False):
    """Recompute and compare against the committed report, then apply the agreed gate."""
    out_dir = Path(out_dir) if out_dir else Path(index_dir or rag_search.DEFAULT_INDEX_DIR)
    committed = out_dir / REPORT_NAME
    report = build_report(index_dir=index_dir)
    blob = report_bytes(report)
    problems = []
    if not committed.exists():
        problems.append(f"{committed} is missing; run python scripts/rag_eval.py")
    elif committed.read_bytes() != blob:
        problems.append(f"{committed} is stale; the current code and eval set produce "
                        "different bytes -- rebuild it and review the diff")
    metrics = report["metrics"]
    ceiling = rag_index.GATE_THRESHOLDS["lexical_recall_at5"]
    if metrics["recall_at5"] < ceiling:
        problems.append(f"recall@{RECALL_AT}={metrics['recall_at5']} is below the agreed "
                        f"ceiling {ceiling}: lexical retrieval is not good enough, and "
                        "that is the number R5's gate reads")
    if metrics["false_answer_rate"] > 0:
        problems.append(f"false_answer_rate={metrics['false_answer_rate']}: out-of-catalog "
                        "questions returned a claimed hit -- the no-answer brake is open")
    if metrics["citation_correctness"] < 1.0:
        problems.append("a returned citation did not resolve back into R1's corpus")
    if metrics["duplicate_hit_rate"] > 0:
        problems.append("duplicate chunks were served for one query (R1 dedup should prevent it)")
    if not quiet:
        print(f"rag_eval: {report['counts']['cases']} cases · "
              f"recall@{RECALL_AT}={metrics['recall_at5']} · "
              f"precision@1={metrics['precision_at_1']} · "
              f"no-answer={metrics['no_answer_rate']} · false-answer={metrics['false_answer_rate']} · "
              f"citations={metrics['citation_correctness']} · dupes={metrics['duplicate_hit_rate']}")
    return problems, report


# --------------------------------------------------------------------------- self-test
class MetricMath(unittest.TestCase):
    """The scorer's arithmetic, checked against hand-computed expectations.

    These fixtures are deliberately NOT the real index: a metric that only ever agrees
    with the thing it measures proves nothing.
    """

    def setUp(self):
        self.pairs = {("SKLA#prompt", "a"), ("SKLA#starter", "b"), ("SKLB#description", "c")}

    def payload(self, rows, coverage=1.0, decision="deferred"):
        return {"results": [{"citation": citation, "chunk_id": chunk_id, "score": score,
                             "explain": {"matched_weight": weight}}
                            for citation, chunk_id, score, weight in rows],
                "query_stats": {"coverage": coverage},
                "no_answer": {"decision": decision}}

    def test_hit_inside_top_five_counts_as_recall(self):
        row = score_case({"id": "x", "kind": "answerable", "query": "q",
                          "ground_truth": ["SKLA#prompt", "SKLA#starter"]},
                         self.payload([("SKLB#description", "c", 9.0, 1.0),
                                       ("SKLA#prompt", "a", 1.0, 1.0)]), self.pairs)
        self.assertEqual(row["recall_at5"], 1)
        self.assertEqual(row["precision_at_1"], 0, "right skill, wrong row is a precision miss")
        self.assertFalse(row["false_answer"])

    def test_out_of_catalog_hit_is_a_false_answer(self):
        row = score_case({"id": "n", "kind": "no_answer", "query": "q", "ground_truth": None},
                         self.payload([("SKLA#prompt", "a", 0.2, 0.45)]), self.pairs)
        self.assertEqual(row["recall_at5"], 0)
        self.assertTrue(row["false_answer"])
        self.assertTrue(row["inferred_only"], "the weak inferred hit is visible, not hidden")

    def test_empty_index_query_is_not_a_false_answer(self):
        row = score_case({"id": "n", "kind": "no_answer", "query": "q", "ground_truth": None},
                         self.payload([], decision="unsearchable"), self.pairs)
        self.assertEqual(row["recall_at5"], 1)
        self.assertFalse(row["false_answer"])
        self.assertEqual(row["no_answer_decision"], "unsearchable")

    def test_citation_that_is_not_in_the_corpus_fails_correctness(self):
        row = score_case({"id": "x", "kind": "answerable", "query": "q",
                          "ground_truth": ["SKLX#prompt"]},
                         self.payload([("SKLX#prompt", "ghost", 5.0, 1.0)]), self.pairs)
        self.assertFalse(row["citation_correct"])
        # Ranking and integrity are separate axes on purpose: this row ranked correctly,
        # so it counts as a hit, while citation_correctness drops to 0.0 and CI fails.
        # Folding the two together would hide which one broke.
        self.assertEqual(row["recall_at5"], 1)

    def test_duplicate_rows_are_counted_not_silently_deduped(self):
        row = score_case({"id": "x", "kind": "answerable", "query": "q",
                          "ground_truth": ["SKLA#prompt"]},
                         self.payload([("SKLA#prompt", "a", 2.0, 1.0),
                                       ("SKLA#prompt", "a", 2.0, 1.0)]), self.pairs)
        self.assertEqual(row["duplicates"], 1)

    def test_report_is_byte_stable_and_advisory_is_excluded(self):
        # Two runs over the real index must produce identical bytes, and an advisory
        # miss must not move the gated number -- both are properties, not accidents.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "train.json"
            fixture = {"format": EVAL_FORMAT, "cases": [
                {"id": "q1", "kind": "answerable", "query": "بريد مهني",
                 "ground_truth": ["SKL005#prompt", "SKL005#description"]},
                {"id": "q2", "kind": "no_answer", "query": "زبدة الحليب", "ground_truth": None},
                {"id": "q3", "kind": "no_answer", "query": "أريد فكرة وصفة سريعة للعشاء",
                 "ground_truth": None, "advisory": True}]}
            path.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
            first = report_bytes(build_report(cases_path=path))
            second = report_bytes(build_report(cases_path=path))
            report = json.loads(first.decode("utf-8"))
        self.assertEqual(first, second)
        self.assertEqual(report["metrics"]["no_answer_rate"], 1.0,
                         "a gated metric must ignore advisory cases")
        self.assertEqual(report["counts"]["advisory_cases"], 1)
        self.assertEqual(report["counts"]["cases"], 3)
        self.assertEqual(report["metrics"]["recall_at5"], 1.0)
        self.assertEqual(report["metrics"]["no_answer_rate"], 1.0)
        self.assertEqual(report["cases"][2]["returned"], ["SKL004#description", "SKL001#description"],
                         "the advisory case keeps its real (noisy) hits in the report")
        self.assertEqual(report["cases"][2]["recall_at5"], 0)
        self.assertEqual(report["metrics"]["false_answer_rate"], 0.0,
                         "and that noise is what keeps it out of the gate, not out of the file")
        self.assertFalse(any("generated_at" in key for key in report))
        self.assertNotIn('"generated_at', first.decode("utf-8"))
        self.assertNotIn("time", json.loads(first.decode("utf-8")).get("metrics", {}))

    def test_load_cases_refuses_a_drifted_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "train.json"
            for bad, reason in (
                ({"format": "other", "cases": []}, "format"),
                ({"format": EVAL_FORMAT, "cases": []}, "no cases"),
                ({"format": EVAL_FORMAT, "cases": [{"id": "a", "kind": "answerable",
                                                    "query": "q", "ground_truth": []}]},
                 "at least one citation"),
                ({"format": EVAL_FORMAT, "cases": [{"id": "a", "kind": "no_answer",
                                                    "query": "q", "ground_truth": ["X#p"]}]},
                 "ground_truth: null"),
                ({"format": EVAL_FORMAT, "cases": [{"id": "a", "kind": "answerable",
                                                    "query": "q", "ground_truth": ["X#p"]},
                                                   {"id": "a", "kind": "answerable",
                                                    "query": "q", "ground_truth": ["X#p"]}]},
                 "duplicate case id"),
            ):
                path.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
                with self.assertRaises(EvalError, msg=reason):
                    load_cases(path)


def self_test():
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(MetricMath)
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    return [] if result.wasSuccessful() else [f"{result.failures} {result.errors}"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=None, help=f"where {REPORT_NAME} is written/read")
    parser.add_argument("--index-dir", default=None, help="index to score against (tests)")
    parser.add_argument("--cases", default=str(TRAIN_PATH))
    parser.add_argument("--check", action="store_true", help="compare + enforce the gate, write nothing")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)

    if args.self_test:
        failures = self_test()
        if failures:
            print("rag_eval self-test FAILED:", failures, file=sys.stderr)
            return 1
        print("rag_eval self-test: ok (recall, precision, false-answer, citation integrity, "
              "duplicates, byte stability, fixture validation)")
        return 0

    out_dir = Path(args.out) if args.out else Path(args.index_dir or rag_search.DEFAULT_INDEX_DIR)
    if args.check:
        problems, report = check(index_dir=args.index_dir, out_dir=out_dir)
        if problems:
            for problem in problems:
                print("rag_eval FAILED:", problem, file=sys.stderr)
            return 1
        print(f"rag_eval: committed {REPORT_NAME} verified · "
              f"recall@{RECALL_AT}={report['metrics']['recall_at5']} · gate ceiling "
              f"{rag_index.GATE_THRESHOLDS['lexical_recall_at5']}")
        return 0

    report = build_report(index_dir=args.index_dir, cases_path=Path(args.cases))
    target = out_dir / REPORT_NAME
    blob = report_bytes(report)
    if target.exists() and target.read_bytes() == blob:
        print(f"unchanged -> {target.relative_to(ROOT) if ROOT in target.parents else target}"
              f" ({len(blob)} bytes)")
    else:
        target.write_bytes(blob)
        print(f"wrote {target.relative_to(ROOT) if ROOT in target.parents else target}"
              f" ({len(blob)} bytes)")
    metrics = report["metrics"]
    for key in METRIC_KEYS:
        print(f"  {key:22} {metrics[key]}")
    misses = [row for row in report["cases"] if row["recall_at5"] == 0 and not row["advisory"]]
    for row in misses:
        print(f"  miss {row['id']}: {row['query']} → {row['returned'] or 'لا نتيجة'}")
    for row in report["cases"]:
        if row["advisory"] and row["recall_at5"] == 0:
            print(f"  advisory {row['id']}: {row['query']} → {row['returned']} "
                  "(مقيسة، غير مُبَوَّبة)")
    if misses:
        print(f"  {len(misses)} من {report['counts']['cases']} حالة لم تُجب -- مفقودة عمدًا؛ "
              "الحذف لرفع الرقم يحوّل المقياس إلى ديكور")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
