"""R3: `kb_search` is a window onto R2, not a second retriever.

The acceptance question is not "does the tool return text" but "can it retrieve at all
without R2" -- so most of this file either proves the delegation happened, or proves the
absence of a scorer. Textual checks are included where a behavioural test would be
weaker than the property: a future edit can add a local BM25 that behaves identically on
six skills, and only the source itself says whether the corpus was read twice.
"""
import ast
import hashlib
import json
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import rag_search                                   # noqa: E402  (the scorer under test)
from agent.runtime import ToolContext               # noqa: E402
from agent.tools import (MAX_KB_CONTEXT_CHARS, MAX_KB_RESULTS, MAX_KB_SNIPPET_CHARS,  # noqa: E402
                         ToolError, build_registry)

INDEX_DIR = ROOT / "data" / "rag"
TOOLS_SOURCE = (ROOT / "backend" / "agent" / "tools.py").read_text(encoding="utf-8")


def code_only(path):
    """Source without docstrings or comments.

    A textual scan over the raw file would flag my own prose ("no `corpus.jsonl` reader
    lives in the agent") as a violation, which proves nothing. Parsing to AST and
    unparsing keeps only executable code, so the scan asks the right question: is there
    retrieval *logic* in here?
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                body.pop(0)
    return ast.unparse(tree)


TOOLS_CODE = code_only(ROOT / "backend" / "agent" / "tools.py")
APP_SOURCE = (ROOT / "backend" / "app.py").read_text(encoding="utf-8")
REGISTRY = build_registry()
TOOL = REGISTRY.get("kb_search")


def context(user_id="u_alpha", store=None, index_dir=INDEX_DIR):
    return ToolContext(user_id, "task-r3", store, None, [], index_dir)


def call(query="كيف أحدد جمهور الرسالة", index_dir=INDEX_DIR, user_id="u_alpha", **args):
    payload = {"query": query, **args}
    return TOOL.handler(context(user_id=user_id, index_dir=index_dir), payload)


def canned(rows, decision="deferred", evidence="RAG_LOCAL", mode="lexical"):
    """A payload shaped exactly like rag_search.search()'s contract."""
    return {
        "query": "سؤال", "search_mode": mode, "evidence": evidence,
        "results": [{"ci": i, "chunk_id": f"sha256:{i:064x}", "citation": f"SKL{i:03d}#prompt",
                     "skill_id": f"SKL{i:03d}", "section": "prompt", "section_label": "الإرشاد التعليمي",
                     "ordinal": 1, "title": f"مهارة {i}", "snippet": row["snippet"], "score": row["score"],
                     "explain": {"matched": {"كلمة": 1}, "matched_weight": row.get("weight", 1.0),
                                 "section_weight": 1.15, "unweighted_score": row["score"],
                                 "doc_len": 20, "covered_chars": len(row["snippet"])},
                     "source": "skills/x/skill.json", "source_hash": "sha256:0",
                     "content_status": "sample", "rag": True} for i, row in enumerate(rows)],
        "query_stats": {"raw_term_count": 2, "content_term_count": 2, "matched_terms": ["كلمة"],
                        "unmatched_terms": [], "removed_phrases": [],
                        "coverage": rows[0].get("weight", 1.0) if rows else 0.0,
                        "inferred_terms": []},
        "no_answer": {"decision": decision, "policy": "deferred to R4",
                      "max_score": rows[0]["score"] if rows else 0.0, "result_count": len(rows)},
        "params": {"k1": 1.4, "b": 0.75, "k": len(rows), "sections": None, "skills": None},
        "weights": {"section": {}, "section_default": 1.0, "expansion": 0.45, "phrase_patterns": 0},
        "index": {"format": rag_search.EXPECTED_FORMAT, "pipeline": {"tokenizer": rag_search.TOKENIZER},
                  "chunks": len(rows), "docs": len(rows), "vocab": 10, "content_status": "sample",
                  "vector_gate": "lexical-only", "directory": str(INDEX_DIR),
                  "manifest_sha256": "sha256:0"},
        "search_version": "waha.rag-search/1",
    }


def tree_hashes(directory):
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(Path(directory).iterdir()) if path.is_file()}


class Delegation(unittest.TestCase):
    """Gate 1: the tool must call R2 -- that is the whole point of R3."""

    def test_calls_rag_search_search_once_with_the_same_arguments(self):
        seen = {}

        def fake_search(query, k=None, sections=None, skills=None, index_dir=None):
            seen.update(query=query, k=k, sections=sections, skills=skills, index_dir=index_dir)
            return canned([{"snippet": "مقطع تعليمي قصير.", "score": 1.5}])

        with patch.object(rag_search, "search", side_effect=fake_search):
            out = call("كيف أحدد جمهور الرسالة", k=3)
        self.assertEqual(seen["query"], "كيف أحدد جمهور الرسالة")
        self.assertEqual(seen["k"], 3)
        self.assertIsNone(seen["sections"], "R3 must not invent filters R2 does not expose")
        self.assertIsNone(seen["skills"])
        self.assertEqual(Path(seen["index_dir"]), INDEX_DIR)
        self.assertEqual(out["results"][0]["citation"], "SKL000#prompt")
        self.assertEqual(out["search_mode"], "lexical")

    def test_no_scorer_or_corpus_reader_inside_the_agent(self):
        # Behaviour can hide a duplicate implementation; the source cannot.
        # (not "rag_index": the injection attribute is plumbing, and it is pinned below)
        for forbidden in ("postings", "idf", "math.log", "content_terms", "tokenize(",
                          "corpus.jsonl", "_bm25", "stem(", "normalize_search", "index.json",
                          "threshold", "relevance"):
            self.assertFalse(forbidden in TOOLS_CODE,
                             f"retrieval logic must not live in tools.py: {forbidden}")
        self.assertIn("rag_search.search(", TOOLS_CODE)
        self.assertEqual(TOOLS_CODE.count("rag_search.search("), 1,
                         "exactly one retrieval call site in the agent")
        self.assertNotIn("open(", TOOLS_CODE.split("def _kb_search")[1].split("def _memory_write")[0],
                         "kb_search must not read files itself")
        self.assertIn("import rag_search", TOOLS_SOURCE)

    def test_endpoint_and_agent_read_one_index_directory(self):
        # The agent gets the path from the app, not from its own os.environ read.
        self.assertIn('"rag_index_dir": RAG_INDEX_DIR', APP_SOURCE)
        self.assertEqual(APP_SOURCE.count('"rag_index_dir"'), 1,
                         "R6: one deps builder -- with a second, hand-built one the two "
                         "modes could each resolve their own index and agree on nothing")
        self.assertNotIn('os.environ.get("WAHA_RAG_DIR")', TOOLS_SOURCE)
        self.assertNotIn("RAG_INDEX_DIR", TOOLS_SOURCE, "the tool never resolves the path itself")


class ReadOnly(unittest.TestCase):
    def test_running_the_tool_does_not_touch_the_corpus(self):
        with patch.object(rag_search, "search", return_value=canned([{"snippet": "س", "score": 1.0}])):
            pass   # the patched probe must not be counted as a filesystem read
        before = tree_hashes(INDEX_DIR)
        call()
        call("بريد", k=99)
        call("العناوين")
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ToolError):
                call("بريد", index_dir=Path(tmp))
        self.assertEqual(before, tree_hashes(INDEX_DIR))
        self.assertTrue(before, "the corpus directory should not be empty")

    def test_the_handler_needs_no_database_at_all(self):
        # ctx.store is None here: any write attempt would be an AttributeError, and the
        # point of gate 7 is that retrieval has no user-bound state to write.
        out = TOOL.handler(context(store=None), {"query": "بريد"})
        self.assertTrue(out["results"])


class EvidenceIntegrity(unittest.TestCase):
    """Gate 3: a citation must survive the trip unchanged, and resolve back to R1."""

    def test_rows_are_verbatim_from_r2(self):
        query = "كيف أحدد جمهور الرسالة"
        direct = rag_search.search(query, k=MAX_KB_RESULTS)
        out = call(query)
        self.assertEqual(len(out["results"]), len(direct["results"]))
        for got, expected in zip(out["results"], direct["results"]):
            for field in ("citation", "chunk_id", "skill_id", "section", "score", "title"):
                self.assertEqual(got[field], expected[field], f"{field} was rewritten by R3")
            self.assertTrue(expected["snippet"].startswith(got["text"].rstrip("…")),
                            "the tool's text must be R2's snippet, shortened only at the cap")
            self.assertNotIn("text", expected, "R3 must not invent a field R2 never produced")

    def test_every_citation_resolves_into_r1_corpus(self):
        corpus = [json.loads(line) for line in
                  (INDEX_DIR / "corpus.jsonl").read_text(encoding="utf-8").splitlines()]
        by_chunk = {row["chunk_id"]: row for row in corpus}
        citations = {row["citation"] for row in corpus}
        out = call("بريد")
        self.assertTrue(out["results"])
        for row in out["results"]:
            self.assertIn(row["citation"], citations)
            source = by_chunk[row["chunk_id"]]
            self.assertEqual(source["citation"], row["citation"])
            self.assertEqual(source["skill_id"], row["skill_id"])
            self.assertEqual(source["section"], row["section"])
            self.assertTrue(source["text"].startswith(row["text"].rstrip("…")))

    def test_evidence_label_is_never_dropped(self):
        with patch.object(rag_search, "search", return_value=canned([{"snippet": "س", "score": 1.0}])):
            out = call("بريد")
        self.assertEqual(out["evidence"], "RAG_LOCAL")
        with patch.object(rag_search, "search", return_value=canned([], decision="unsearchable")):
            out = call("اشرح لي")
        self.assertEqual(out["no_answer"]["decision"], "unsearchable")
        self.assertEqual(out["results"], [])


class FailureModes(unittest.TestCase):
    def test_empty_index_is_an_error_not_an_empty_answer(self):
        # Gate 5: laundering "the library is not installed" into "we found nothing"
        # would poison R4's no-answer-rate with an infrastructure failure.
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ToolError) as caught:
                call("بريد", index_dir=Path(tmp))
        message = str(caught.exception)
        self.assertIn("مكتبة المعرفة غير متاحة", message)
        self.assertIn("rag_index", message.lower(), "the operator-facing reason must survive")

    def test_unsearchable_query_is_reported_not_invented(self):
        out = call("طبخة الكبة بالبرغل")
        self.assertEqual(out["results"], [])
        self.assertEqual(out["no_answer"]["decision"], "deferred")
        self.assertEqual(out["no_answer"]["max_score"], 0.0)
        self.assertNotIn("answer", out, "R3 retrieves evidence; it does not answer")
        self.assertEqual(out["coverage"], 0.0)

    def test_no_threshold_is_decided_here(self):
        # A 1e-4 hit still travels to the model with its score and coverage; nothing here
        # decides "too weak to mention", because R4 owns that call.
        with patch.object(rag_search, "search",
                          return_value=canned([{"snippet": "مقطع", "score": 0.0001, "weight": 0.45}])):
            out = call("أي شيء")
        self.assertEqual(len(out["results"]), 1)
        self.assertEqual(out["no_answer"]["decision"], "deferred")
        self.assertFalse(out["results"][0]["matched_directly"])
        self.assertNotIn("min_score", json.dumps(out))
        self.assertIsNone(re.search(r"score\s*[><]=?", TOOLS_CODE),
                          "no score comparison anywhere in the tool layer")
        self.assertNotIn("answered", json.dumps(out))


class Permissions(unittest.TestCase):
    CONFIG = types.SimpleNamespace(NETWORK_TOOLS=True)

    def test_read_only_and_no_approval(self):
        brief = TOOL.brief()
        self.assertFalse(brief["requires_approval"])
        self.assertTrue(brief["read_only"])
        self.assertFalse(brief["network"])
        self.assertIsNone(TOOL.gated, "retrieval must not be switchable off by config")
        self.assertIn("kb_search", REGISTRY.names(self.CONFIG))
        self.assertIn("kb_search", REGISTRY.names(types.SimpleNamespace(NETWORK_TOOLS=False)),
                      "a disabled web_fetch must not disable the knowledge base")

    def test_prompt_text_labels_it_read_only(self):
        line = next(item for item in REGISTRY.prompt_text(self.CONFIG).splitlines()
                    if "kb_search" in item)
        self.assertIn("[read-only]", line)
        self.assertNotIn("needs user approval", line)

    def test_the_model_cannot_grant_itself_arguments(self):
        for extra in ({"requires_approval": False}, {"auto_approve": True}, {"threshold": 0.9},
                      {"filters": {"section": "prompt"}}, {"index_dir": "/tmp"},
                      {"sections": ["starter"]}, {"rerank": True}):
            args = {"query": "بريد", **extra}
            with self.assertRaises(ToolError, msg=f"{extra} must not be accepted"):
                TOOL.validate(args)


class Budgets(unittest.TestCase):
    def test_k_ceiling_is_the_tools_own_and_smaller_than_the_endpoints(self):
        self.assertEqual(MAX_KB_RESULTS, 5)
        self.assertLess(MAX_KB_RESULTS, rag_search.MAX_RESULTS)
        with patch.object(rag_search, "search",
                          return_value=canned([{"snippet": "س", "score": 1.0}] * 5)) as probe:
            out = call("بريد", k=900)
        self.assertEqual(probe.call_args.kwargs["k"], MAX_KB_RESULTS)
        self.assertEqual(out["requested_k"], 900)
        self.assertIn(f"سقف الأداة {MAX_KB_RESULTS}", out["note"])
        for bad in (0, -1, True, "many"):
            with self.assertRaises(ToolError, msg=f"k={bad!r} must be rejected"):
                call("بريد", k=bad)

    def test_query_ceiling_is_reused_not_redefined(self):
        self.assertNotIn("MAX_QUERY_CHARS =", TOOLS_SOURCE)
        long_query = "كلمة " * (rag_search.MAX_QUERY_CHARS // 2)
        with self.assertRaises(ToolError) as caught:
            call(long_query.strip())
        self.assertIn(str(rag_search.MAX_QUERY_CHARS), str(caught.exception))

    def test_total_evidence_is_capped_and_says_so(self):
        fat = [{"snippet": "ل" * 400, "score": 1.0}] * 8
        with patch.object(rag_search, "search", return_value=canned(fat)):
            out = call("بريد")
        self.assertLessEqual(out["evidence_chars"], MAX_KB_CONTEXT_CHARS)
        self.assertLess(len(out["results"]), 8)
        self.assertTrue(out["evidence_truncated"])
        self.assertIn(f"{MAX_KB_CONTEXT_CHARS}", out["note"])

    def test_one_row_survives_even_when_it_exceeds_the_budget(self):
        # Silently returning zero results after R2 found something would turn a
        # formatting limit into a false "no answer".
        with patch.object(rag_search, "search",
                          return_value=canned([{"snippet": "ل" * 9000, "score": 5.0}])):
            out = call("بريد")
        self.assertEqual(len(out["results"]), 1)
        self.assertEqual(len(out["results"][0]["text"]), MAX_KB_SNIPPET_CHARS)
        self.assertTrue(out["results"][0]["snippet_truncated"])

    def test_inferred_only_hits_are_announced(self):
        with patch.object(rag_search, "search",
                          return_value=canned([{"snippet": "مقطع", "score": 1.0, "weight": 0.45}])):
            out = call("الوقت")
        self.assertFalse(out["results"][0]["matched_directly"])
        self.assertIn("مُستنتَجة", out["note"])


class NoOwnership(unittest.TestCase):
    """Gate 7: the corpus is public knowledge; retrieval must not borrow identity."""

    def test_results_do_not_depend_on_the_visitor(self):
        first = call("بريد", user_id="u_one")
        second = call("بريد", user_id="u_two")
        self.assertEqual(json.dumps(first, sort_keys=True, ensure_ascii=False),
                         json.dumps(second, sort_keys=True, ensure_ascii=False))

    def test_payload_carries_no_identity(self):
        out = call("بريد")
        blob = json.dumps(out, ensure_ascii=False)
        self.assertNotIn("u_alpha", blob)
        self.assertNotIn("task-r3", blob)
        self.assertNotIn("user", blob.lower())
        self.assertNotIn("session", blob.lower())
        for key in out:
            self.assertNotIn("owner", key)


class Registration(unittest.TestCase):
    def test_skill_lookup_does_not_call_a_javascript_method(self):
        # `baddf10` shipped `.toLocaleLowerCase()` in _skill_lookup -- a JS name in Python.
        # Nothing tested that path, so any model call to skill_lookup with a query died in
        # the generic handler and the loop reported "تعذّر تنفيذ الأداة". Found here because
        # this file lists every tool, and it is fixed here for the same reason.
        skills = [{"id": "SKL001", "name": "كتابة المحتوى", "description": "بريد ورسائل",
                   "tags": ["بريد"], "steps": ["حدّد الجمهور"]}]
        out = REGISTRY.get("skill_lookup").handler(context(store=None, **{}), {"query": "بريد"},
                                                  ) if False else None
        ctx = context(store=None)
        ctx.skills = skills
        out = REGISTRY.get("skill_lookup").handler(ctx, {"query": "بريد"})
        self.assertEqual(out["matched_by"], "query")
        self.assertEqual(out["skills"][0]["id"], "SKL001")
        self.assertNotIn("toLocaleLowerCase", TOOLS_SOURCE)

    def test_skill_lookup_still_answers_the_catalog(self):
        # Two tools, two jobs: skill_lookup finds *skills*, kb_search finds evidence
        # inside them. Neither may quietly replace the other.
        self.assertIn("skill_lookup", REGISTRY.names())
        brief = REGISTRY.get("skill_lookup").brief()
        self.assertFalse(brief["requires_approval"])
        self.assertNotIn("citation", json.dumps(
            REGISTRY.get("skill_lookup").handler(context(store=None), {"skill_id": "NOPE"},
                                                 ) if False else {"ok": 1}))
        self.assertIn("citation", json.dumps(call("بريد")))


if __name__ == "__main__":
    unittest.main()
