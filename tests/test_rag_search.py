"""R2: search is the only consumer of R1's index, and it never rewrites it.

Every assertion here is about the *contract*, not about impressions of relevance:
citations must resolve back into data/rag/, the R1 files must be byte-identical
afterwards, ranking must be reproducible, and "answered?" must stay an open
question for R4 instead of a threshold invented here.
"""
import hashlib
import importlib.util
import json
import os
import sys
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))
import rag_index  # noqa: E402  (R1 builder, used to make synthetic indexes the same way)
import rag_search  # noqa: E402

R1_DIR = ROOT / "data/rag"
R1_FILES = ("index.json", "corpus.jsonl", "manifest.json", "stats.json")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fingerprints(directory=R1_DIR):
    return {name: digest(Path(directory) / name) for name in R1_FILES}


def synthetic_index(directory, *skills):
    """Build an index through R1's own builder, so weights are tested on real R1 bytes."""
    sources = {row["id"]: {"path": f"skills/{row['id']}/skill.json", "bytes": 1,
                           "sha256": "sha256:" + "0" * 64} for row in skills}
    corpus, index, manifest, stats, _sections = rag_index.build(list(skills), sources, "sample")
    files = rag_index.render(corpus, index, manifest, stats)
    directory.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (directory / name).write_text(content, encoding="utf-8")
    return directory


def skill(skill_id, **overrides):
    base = {"id": skill_id, "name": f"مهارة {skill_id}", "category": "اختبار", "difficulty": "مبتدئ",
            "icon": "pen", "color": "peach", "description": "وصف قصير.",
            "starter": "مثال قصير.", "prompt": "إرشاد قصير.", "tags": ["اختبار"]}
    base.update(overrides)
    return base


class Contract(unittest.TestCase):
    def test_known_query_returns_the_expected_skill(self):
        payload = rag_search.search("متى أستخدم الوسيط بدل المتوسط", k=5)
        self.assertEqual(payload["evidence"], "RAG_LOCAL")
        self.assertEqual(payload["search_mode"], "lexical")
        self.assertTrue(payload["results"], "expected at least one hit")
        self.assertEqual(payload["results"][0]["skill_id"], "SKL006")
        self.assertIn("الوسيط", payload["results"][0]["snippet"])

    def test_citations_resolve_back_into_r1_without_regeneration(self):
        rows = {}
        for line in (R1_DIR / "corpus.jsonl").read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            rows[record["citation"]] = record
        index = json.loads((R1_DIR / "index.json").read_text(encoding="utf-8"))
        seen = set()
        for query in ("ترتيب الأولويات", "كتابة بريد مهني", "تجربة مستخدم RTL", "تحليل بيانات",
                      "أساسيات Python", "كتابة محتوى", "اشرح لي الفرق بين المتوسط والوسيط"):
            for row in rag_search.search(query, k=5)["results"]:
                source = rows[row["citation"]]
                seen.add(row["citation"])
                self.assertEqual(source["chunk_id"], row["chunk_id"], row["citation"])
                self.assertEqual(source["source_hash"], row["source_hash"], row["citation"])
                self.assertEqual(source["skill_id"], row["skill_id"], row["citation"])
                # the snippet is R1's text, truncated -- never re-tokenized or rewritten
                self.assertTrue(source["text"].startswith(row["snippet"].rstrip("…")), row["citation"])
                self.assertIn(row["chunk_id"], {chunk["chunk_id"] for chunk in index["chunks"]})
        self.assertGreaterEqual(len(seen), 4, "the probe queries must actually touch the index")

    def test_searching_never_touches_the_r1_artifacts(self):
        before = fingerprints()
        for query in ("بريد", "تصميم", "وقت", "بيانات", "python", "محتوى", "اشرح لي", "xyzzy"):
            rag_search.search(query, k=8)
            rag_search.search(query, k=2, sections=["prompt"])
        self.assertEqual(before, fingerprints(), "R2 must be strictly read-only over data/rag")

    def test_output_is_deterministic_and_ties_break_by_ci(self):
        first = rag_search.search("أولويات وخطة يومية", k=8)
        second = rag_search.search("أولويات وخطة يومية", k=8)
        self.assertEqual(json.dumps(first, ensure_ascii=False, sort_keys=True),
                         json.dumps(second, ensure_ascii=False, sort_keys=True))
        scores = [row["score"] for row in first["results"]]
        cis = [row["ci"] for row in first["results"]]
        self.assertEqual(scores, sorted(scores, reverse=True))
        for left, right in zip(first["results"], first["results"][1:]):
            if left["score"] == right["score"]:
                self.assertLess(left["ci"], right["ci"])
        self.assertEqual(len(cis), len(set(cis)))

    def test_out_of_catalog_query_claims_nothing(self):
        for query in ("ما سعر صرف الليرة اليوم", "كيف أركّب لوحة كهربائية في السيارة",
                      "طبخة الكبة بالبرغل"):
            payload = rag_search.search(query, k=5)
            self.assertEqual(payload["results"], [], f"{query} must not fabricate a source")
            self.assertEqual(payload["no_answer"]["decision"], "deferred")
            self.assertEqual(payload["no_answer"]["max_score"], 0.0)
            self.assertIsNone(payload["params"]["sections"])

    def test_no_answer_threshold_belongs_to_r4(self):
        # A weak hit is *reported*, never promoted to "answered" and never hidden
        # behind a threshold invented at query time.
        payload = rag_search.search("بريد", k=3)
        self.assertTrue(payload["results"])
        sections = [row["section"] for row in payload["results"]]
        self.assertEqual(sections[0], "prompt",
                         "real-corpus ranking must prefer the instruction over the example")
        self.assertIn("starter", sections)
        self.assertEqual(payload["no_answer"]["decision"], "deferred")
        self.assertIn("eval-report.json", payload["no_answer"]["policy"])
        for row in payload["results"]:
            self.assertIn("explain", row)
            self.assertIsInstance(row["explain"]["unweighted_score"], float)
            self.assertGreater(row["explain"]["doc_len"], 0)
        # A word that exists nowhere in the corpus stays a non-answer: zero hits, no
        # invented source, and max_score 0.0 for R4 to read. (The title-only case
        # «العناوين» is the honest zero; the measured article-fold case is below.)
        miss = rag_search.search("العناوين", k=3)
        self.assertEqual(miss["results"], [])
        self.assertEqual(miss["no_answer"]["decision"], "deferred")
        self.assertEqual(miss["no_answer"]["max_score"], 0.0)

    def test_definite_article_fold_is_inferred_and_labelled(self):
        # «الوقت» is not a posting key; «وقت» is. The one documented inference (drop the
        # definite article) reaches it, and the row says out loud that it was inferred.
        payload = rag_search.search("الوقت", k=3)
        self.assertEqual([row["citation"] for row in payload["results"]], ["SKL003#prompt"])
        row = payload["results"][0]
        self.assertEqual(row["explain"]["matched"], {"وقت": 1})
        self.assertEqual(row["explain"]["matched_weight"], rag_search.EXPANSION_WEIGHT)
        self.assertEqual(payload["query_stats"]["coverage"], 0.0)
        self.assertEqual(payload["query_stats"]["unmatched_terms"], ["الوقت"])
        self.assertEqual(payload["query_stats"]["inferred_terms"], ["وقت"])

    def test_query_stemming_is_not_run_at_query_time(self):
        # Measured reason for the limit: the full affix-lite stem folded «اليوم» to «يوم»
        # and R1's expansion set then produced «يوميه/يومين», scoring a currency question
        # onto a time-management skill. Over-stemming is a precision bug, so it stays out.
        for query in ("ما سعر صرف الليرة اليوم", "اليوم", "سعر الليرة"):
            payload = rag_search.search(query, k=5)
            self.assertEqual(payload["results"], [], f"{query} must not be answered")
            self.assertNotIn("يوميه", payload["query_stats"]["matched_terms"])
            self.assertNotIn("يومين", payload["query_stats"]["matched_terms"])
            self.assertEqual(payload["no_answer"]["max_score"], 0.0)

    def test_phatic_query_alone_is_unsearchable_not_answered(self):
        payload = rag_search.search("اشرح لي من فضلك")
        self.assertEqual(payload["results"], [])
        self.assertEqual(payload["no_answer"]["decision"], "unsearchable")
        self.assertEqual(payload["query_stats"]["content_term_count"], 0)
        self.assertTrue(payload["query_stats"]["removed_phrases"])

    def test_stem_expansion_hit_reports_zero_coverage(self):
        # The corpus says «الجمهور»; a query saying «جمهور» reaches it only through
        # R1's expansion dict at EXPANSION_WEIGHT. Coverage counts direct matches,
        # so an inferred-only hit must stay 0.0 -- that gap is R4's evidence.
        payload = rag_search.search("كيف أحدد جمهور الرسالة", k=3)
        self.assertTrue(payload["results"])
        self.assertEqual(payload["query_stats"]["coverage"], 0.0)
        self.assertIn("الجمهور", payload["query_stats"]["matched_terms"])
        self.assertTrue(all(row["explain"]["matched_weight"] <= rag_search.EXPANSION_WEIGHT
                             for row in payload["results"]))

    def test_health_shape_never_pretends_vectors_exist(self):
        status = rag_search.status()
        self.assertTrue(status["ready"])
        self.assertEqual(status["mode"], "lexical")
        self.assertEqual(status["content_status"], "sample")
        self.assertEqual(status["vector_gate"], "lexical-only")
        self.assertEqual(status["chunks"], 18)


class SectionWeights(unittest.TestCase):
    def test_starter_cannot_outrank_prompt_on_equal_overlap(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Same tokens on purpose; the wording must differ or R1 dedups one copy
            # out of the index entirely.
            directory = synthetic_index(Path(tmp) / "rag",
                                        skill("SKLA", starter="المقارنة بين التفاح والبرتقال تحتاج جدولاً"),
                                        skill("SKLB", prompt="المقارنة بين التفاح والبرتقال تستحق جدولاً"))
            payload = rag_search.search("المقارنة بين التفاح والبرتقال", k=5,
                                        index_dir=str(directory))
            top = payload["results"][0]
            self.assertEqual(top["skill_id"], "SKLB", "example prompts are not stronger evidence")
            self.assertEqual(top["explain"]["section_weight"], rag_search.SECTION_WEIGHTS["prompt"])
            self.assertEqual(top["citation"], "SKLB#prompt")
            unweighted = {row["skill_id"]: row["explain"]["unweighted_score"] for row in payload["results"]}
            self.assertAlmostEqual(unweighted["SKLA"], unweighted["SKLB"], places=9,
                                   msg="weights must only reorder, never change the base match")
            self.assertGreater(top["score"], unweighted["SKLB"])

    def test_unknown_sections_keep_neutral_weight(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = synthetic_index(Path(tmp) / "rag",
                                        skill("SKLA", sections=[{"title": "خطوات",
                                                                 "body": "قِس النتيجة قبل التحسين."}]))
            payload = rag_search.search("قِس النتيجة قبل التحسين", k=3, index_dir=str(directory))
            self.assertEqual(payload["results"][0]["explain"]["section_weight"],
                             rag_search.DEFAULT_SECTION_WEIGHT)
            self.assertEqual(payload["results"][0]["citation"], "SKLA#section1")

    def test_filters_restrict_the_candidate_set_deterministically(self):
        only_prompt = rag_search.search("الوسيط", k=5, sections=["prompt"])
        self.assertTrue(all(row["section"] == "prompt" for row in only_prompt["results"]))
        only_other = rag_search.search("الوسيط", k=5, sections=["description"])
        self.assertEqual(only_other["results"], [])
        by_skill = rag_search.search("الوسيط", k=5, skills=["SKL006"])
        self.assertTrue(all(row["skill_id"] == "SKL006" for row in by_skill["results"]))
        wrong_skill = rag_search.search("الوسيط", k=5, skills=["SKL001"])
        self.assertEqual(wrong_skill["results"], [])
        self.assertEqual(by_skill["params"]["skills"], ["SKL006"])


class SharedFolding(unittest.TestCase):
    """R1 indexes and R2 query with the *same* functions, or nothing else matters."""

    def test_both_stages_report_one_tokenizer_version(self):
        import rag_text
        self.assertEqual(rag_index.PIPELINE["tokenizer"], rag_text.TOKENIZER)
        self.assertEqual(rag_index.PIPELINE["stemmer"], rag_text.STEMMER)
        self.assertEqual(rag_search.TOKENIZER, rag_text.TOKENIZER)
        self.assertEqual(rag_index.FORMAT, rag_search.EXPECTED_FORMAT)
        self.assertIs(rag_index.content_terms, rag_text.content_terms)
        self.assertIs(rag_search.content_terms, rag_text.content_terms)

    def test_indexed_postings_are_reproducible_from_stored_text(self):
        # Every (term, chunk) key in the index must come back out of that chunk's text
        # when folded the same way -- proof the query path sees what was indexed.
        state = rag_search.load()
        rows = state["rows"]
        checked = 0
        for term, entry in sorted(state["index"]["postings"].items()):
            for ci, _tf in sorted(entry["p"]):
                self.assertIn(term, rag_search.content_terms(rows[ci]["text"]),
                              f"{term} indexed but not reproducible from {ci}")
                checked += 1
            if checked > 80:
                break
        self.assertGreater(checked, 40)

    def test_search_works_without_scripts_on_the_path(self):
        # Vercel ships backend/ and data/ but excludes scripts/**: R2 must still answer.
        code = ("import sys; sys.path.insert(0, '.');"
                "import json, rag_search;"
                "print(json.dumps({'status': rag_search.status(),"
                " 'hits': [r['citation'] for r in rag_search.search('بريد')['results']]}))")
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT / "backend",
                                env={"PYTHONPATH": str(ROOT / "backend"), "PATH": "/usr/bin:/bin"},
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertTrue(payload["status"]["ready"], payload)
        self.assertIn("SKL005#prompt", payload["hits"])


class FailClosed(unittest.TestCase):
    def test_missing_index_raises_rather_than_returning_empty_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(rag_search.RagSearchUnavailable) as caught:
                rag_search.search("بريد", index_dir=str(Path(tmp) / "nowhere"))
            self.assertIn("scripts/rag_index.py", str(caught.exception))
            status = rag_search.status(str(Path(tmp) / "nowhere"))
            self.assertFalse(status["ready"])
            self.assertIsNone(status["mode"])

    def test_truncated_index_is_refused_not_half_answered(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = synthetic_index(Path(tmp) / "rag", skill("SKLA"))
            (directory / "corpus.jsonl").write_text('{"ci": 0}\n', encoding="utf-8")
            rag_search._CACHE.clear()
            with self.assertRaises(rag_search.RagSearchUnavailable) as caught:
                rag_search.search("بريد", index_dir=str(directory))
            self.assertIn("corpus.jsonl", str(caught.exception))
            rag_search._CACHE.clear()

    def test_foreign_pipeline_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = synthetic_index(Path(tmp) / "rag", skill("SKLA"))
            index = json.loads((directory / "index.json").read_text(encoding="utf-8"))
            index["pipeline"]["tokenizer"] = "someone-elses-words/9"
            (directory / "index.json").write_text(json.dumps(index, ensure_ascii=False),
                                                   encoding="utf-8")
            rag_search._CACHE.clear()
            with self.assertRaises(rag_search.RagSearchUnavailable) as caught:
                rag_search.search("بريد", index_dir=str(directory))
            self.assertIn("tokenizer", str(caught.exception))
            rag_search._CACHE.clear()

    def test_cache_reloads_when_the_index_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = synthetic_index(Path(tmp) / "rag", skill("SKLA", prompt="كلمة واحدة"))
            rag_search._CACHE.clear()
            self.assertEqual(rag_search.search("كلمة", index_dir=str(directory))["results"][0]
                             ["citation"], "SKLA#prompt")
            synthetic_index(directory, skill("SKLA", prompt="كلمة مغايرة تمامًا"))
            later = int(time.time() * 1e9) + 5_000_000_000
            for name in R1_FILES:
                os.utime(directory / name, ns=(later, later))
            # deliberately no _CACHE.clear(): the signature must notice on its own
            payload = rag_search.search("مغايرة", index_dir=str(directory))
            self.assertTrue(payload["results"])
            self.assertIn("مغايرة", payload["results"][0]["snippet"])
            rag_search._CACHE.clear()


class Endpoint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.environ_backup = {}
        for key in ("GEMINI_API_KEY", "PROMPTQL_PLATFORM_API_URL", "WAHA_MODEL",
                    "AGENT_MODEL", "WAHA_TRUST_PROMPTQL"):
            cls.environ_backup[key] = os.environ.pop(key, None)
        temp = tempfile.TemporaryDirectory()
        cls.temp = temp
        os.environ["WAHA_DB"] = str(Path(temp.name) / "rag-test.db")
        # Read-only search still goes through the shared CORS policy, so the Pages
        # origin has to be the one the app expects.
        cls.environ_backup["WAHA_ALLOWED_ORIGINS"] = os.environ.get("WAHA_ALLOWED_ORIGINS")
        os.environ["WAHA_ALLOWED_ORIGINS"] = "https://pages.test"
        spec = importlib.util.spec_from_file_location("waha_rag_backend", ROOT / "backend/app.py")
        cls.backend = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.backend)
        cls.client = cls.backend.app.test_client()

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("WAHA_DB", None)
        for key, value in cls.environ_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        cls.temp.cleanup()

    def test_query_round_trip(self):
        response = self.client.get("/api/search?q=" + "متى أستخدم الوسيط")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["evidence"], "RAG_LOCAL")
        self.assertEqual(payload["results"][0]["skill_id"], "SKL006")
        # The app sets Cache-Control centrally; a route that forks header policy would
        # be a second source of truth, so R2 asserts the shared one instead.
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_validation_is_explicit(self):
        self.assertEqual(self.client.get("/api/search").status_code, 400)
        self.assertEqual(self.client.get("/api/search?q=").get_json()["code"], "missing_query")
        self.assertEqual(self.client.get("/api/search?q=" + "ك" * 500).get_json()["code"],
                         "query_too_long")
        self.assertEqual(self.client.get("/api/search?q=بريد&k=abc").get_json()["code"], "invalid_k")
        self.assertEqual(self.client.get("/api/search?q=بريد&k=0").get_json()["code"], "invalid_k")

    def test_k_is_clamped_not_unbounded(self):
        payload = self.client.get("/api/search?q=بريد&k=999").get_json()
        self.assertEqual(payload["params"]["k"], rag_search.MAX_RESULTS)
        self.assertEqual(self.client.get("/api/search?q=بريد&k=-3").get_json()["code"],
                         "invalid_k")
        payload = self.client.get("/api/search?q=بريد&k=1").get_json()
        self.assertLessEqual(len(payload["results"]), 1)

    def _table_names(self):
        """Dialect-neutral: CI runs this suite against sqlite *and* Postgres, and a
        hard-coded sqlite_master would only ever exercise one of them."""
        if self.backend.POSTGRES:
            sql = "SELECT tablename AS n FROM pg_tables WHERE schemaname = 'public'"
        else:
            sql = "SELECT name AS n FROM sqlite_master WHERE type = 'table'"
        with self.backend.connect() as db:
            return {row["n"] for row in self.backend.run(db, sql).fetchall()}

    def test_search_writes_nothing_to_the_database(self):
        with self.backend.connect() as db:
            before = self.backend.run(db, "SELECT COUNT(1) AS n FROM attempts").fetchone()["n"]
        for query in ("بريد", "تصميم", "وقت"):
            self.client.get("/api/search?q=" + query)
        with self.backend.connect() as db:
            after = self.backend.run(db, "SELECT COUNT(1) AS n FROM attempts").fetchone()["n"]
        self.assertEqual(before, after, "a search must not consume the chat budget")
        tables = self._table_names()
        self.assertTrue(tables, "the schema should have been created by now")
        parallel = {name for name in tables if any(hint in name for hint in ("rag", "chunk", "corpus", "vector"))}
        self.assertEqual(parallel, set(), "R2 must not create a parallel store beside data/rag")

    def test_health_advertises_the_retrieval_capability_honestly(self):
        payload = self.client.get("/health").get_json()
        self.assertTrue(payload["rag"]["ready"])
        self.assertEqual(payload["rag"]["mode"], "lexical")
        self.assertEqual(payload["rag"]["vector_gate"], "lexical-only")
        self.assertNotIn("embeddings", json.dumps(payload))

    def test_missing_index_is_a_503_not_an_empty_200(self):
        original = self.backend.RAG_INDEX_DIR
        try:
            self.backend.RAG_INDEX_DIR = Path(self.temp.name) / "absent"
            response = self.client.get("/api/search?q=بريد")
            self.assertEqual(response.status_code, 503)
            body = response.get_json()
            self.assertEqual(body["code"], "rag_index_missing")
            self.assertIn("scripts/rag_index.py", body["error"])
            self.assertFalse(self.client.get("/health").get_json()["rag"]["ready"])
        finally:
            self.backend.RAG_INDEX_DIR = original

    def test_no_cross_origin_write_and_no_auth_requirement(self):
        response = self.client.get("/api/search?q=بريد",
                                   headers={"Origin": "https://pages.test"})
        self.assertEqual(response.status_code, 200)
        # POST never reaches a handler: 405 from routing or 401 from the shared write
        # gate. Both mean the same thing here -- search cannot mutate anything.
        self.assertIn(self.client.post("/api/search", json={"q": "بريد"}).status_code, (401, 405))

    def test_index_pin_in_the_response_matches_the_committed_manifest(self):
        manifest = json.loads((R1_DIR / "manifest.json").read_text(encoding="utf-8"))
        pinned = manifest["artifacts"]["index.json"]["sha256"]
        payload = self.client.get("/api/search?q=بريد").get_json()
        self.assertEqual(payload["index"]["manifest_sha256"], pinned)
        self.assertEqual(payload["index"]["format"], rag_index.FORMAT)


if __name__ == "__main__":
    unittest.main()
