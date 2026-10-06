"""R1 contract tests: the RAG corpus must be deterministic, lossless and citable.

These run offline against the real catalog plus synthetic inputs. They are the
reason R2/R3/R4 can trust `data/rag/` instead of re-deriving their own schema.
"""
import ast
import io
import json
import contextlib
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import rag_index  # noqa: E402

OUT = ROOT / "data/rag"
CHUNK_FIELDS = {"ci", "chunk_id", "chunk_hash", "source_hash", "skill_id", "section",
                "section_label", "ordinal", "title", "text", "span", "covered",
                "overlap_blocks", "overlap_chars", "tail_merged", "chars", "covered_chars",
                "tokens", "citation", "indexed", "duplicate_of", "source", "content_status"}


def skill(**overrides):
    base = {"id": "SKL001", "name": "كتابة المحتوى", "category": "كتابة",
            "difficulty": "متوسط", "icon": "pen", "color": "peach",
            "description": "حوّل فكرة إلى نص واضح يناسب جمهورك.",
            "starter": "أريد كتابة منشور قصير عن إطلاق منتج.",
            "prompt": "اسأل عن الجمهور والهدف أولًا.", "tags": ["محتوى"]}
    base.update(overrides)
    return base


def sources(*ids, prefix="SKL001"):
    return {key: {"path": f"skills/{key}/skill.json", "bytes": 12,
                  "sha256": rag_index.sha256_hex(key.encode("utf-8"))}
            for key in (prefix, *ids)}


def build(skills, status="sample", recall=None):
    src = {row["id"]: {"path": f"skills/{row['id']}/skill.json", "bytes": 12,
                       "sha256": rag_index.sha256_hex(row["id"].encode())} for row in skills}
    return rag_index.build(skills, src, status, recall)


def rows_from(chunks, text, section):
    """Turn raw chunk_section() output into contract-complete rows for verify()."""
    rows = []
    for index, chunk in enumerate(chunks):
        rows.append({"ci": index,
                     "chunk_id": rag_index.sha256_hex(f"{index}".encode("utf-8")),
                     "chunk_hash": rag_index.sha256_hex(text[chunk["span"][0]:chunk["span"][1]].encode("utf-8")),
                     "source_hash": "sha256:" + "0" * 64, "skill_id": "S", "section": section,
                     "section_label": section, "ordinal": index + 1, "title": "t",
                     "text": text[chunk["span"][0]:chunk["span"][1]], "span": chunk["span"],
                     "covered": chunk["covered"], "overlap_blocks": chunk["overlap_blocks"],
                     "overlap_chars": chunk["overlap_chars"], "tail_merged": chunk["tail_merged"],
                     "chars": chunk["chars"], "covered_chars": chunk["covered_chars"],
                     "tokens": 1, "citation": f"S#{section}:{index + 1}", "indexed": True,
                     "duplicate_of": None, "source": "skills/S/skill.json",
                     "content_status": "sample"})
    return rows


def quiet(fn, *args, **kwargs):
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        code = fn(*args, **kwargs)
    return code, buffer.getvalue()


class CommittedArtifacts(unittest.TestCase):
    def test_self_test_proves_the_contract(self):
        code, output = quiet(rag_index.main, ["--self-test"])
        self.assertEqual(code, 0, output)

    def test_staged_artifacts_are_current(self):
        code, output = quiet(rag_index.main, ["--check"])
        self.assertEqual(code, 0, output)

    def test_all_four_files_exist_and_are_non_empty(self):
        for name in ("corpus.jsonl", "index.json", "manifest.json", "stats.json"):
            path = OUT / name
            self.assertTrue(path.exists(), name)
            self.assertGreater(path.stat().st_size, 100, name)
            self.assertTrue(path.read_text(encoding="utf-8").endswith("\n"), name)

    def test_cli_writes_then_checks_without_touching_the_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = str(Path(tmp) / "rag")
            code, output = quiet(rag_index.main, ["--out", out, "--print-stats", "--verify"])
            self.assertEqual(code, 0, output)
            self.assertIn("contract verified", output)
            code, output = quiet(rag_index.main, ["--out", out, "--check"])
            self.assertEqual(code, 0, output)
            # a fresh build must be indistinguishable from the committed artifacts
            for name in ("corpus.jsonl", "index.json", "manifest.json", "stats.json"):
                self.assertEqual((Path(out) / name).read_bytes(), (OUT / name).read_bytes(), name)
            # and a hand-edited artifact is caught
            (Path(out) / "index.json").write_text((Path(out) / "index.json").read_text() + " ",
                                                   encoding="utf-8")
            code, output = quiet(rag_index.main, ["--out", out, "--check"])
            self.assertEqual(code, 1)
            self.assertIn("outdated index.json", output)

    def test_r1_and_r2_share_one_folder(self):
        # The extraction of backend/rag_text.py must not have changed what R1 writes.
        import rag_text
        self.assertIs(rag_index.normalize_search, rag_text.normalize_search)
        self.assertIs(rag_index.stem, rag_text.stem)
        for name in ("index.json", "corpus.jsonl", "manifest.json", "stats.json"):
            self.assertTrue((OUT / name).exists(), name)

    def test_stdlib_only_imports(self):
        # stdlib plus the two first-party modules R1 must share (the catalog loader,
        # and the folding rules R2 queries with). Anything else is a new dependency.
        allowed = {"argparse", "hashlib", "json", "re", "sys", "unicodedata", "pathlib",
                   "build_catalog", "rag_text"}
        tree = ast.parse((ROOT / "scripts/rag_index.py").read_text(encoding="utf-8"))
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                found.add(node.module.split(".")[0])
        self.assertEqual(sorted(found - allowed), [], "R1 must not gain a dependency")


class Determinism(unittest.TestCase):
    def test_two_builds_are_byte_identical(self):
        first = rag_index.render(*build([skill()])[:4])
        second = rag_index.render(*build([skill()])[:4])
        self.assertEqual(first, second)

    def test_writing_to_two_directories_produces_identical_bytes(self):
        skills, srcs, status = rag_index.collect_skills()
        payloads = {}
        for tag in ("a", "b"):
            with tempfile.TemporaryDirectory() as tmp:
                code, output = quiet(rag_index.main, ["--out", str(Path(tmp) / tag)])
                self.assertEqual(code, 0, output)
                payloads[tag] = {path.name: path.read_bytes()
                                 for path in (Path(tmp) / tag).glob("*")}
        self.assertEqual(payloads["a"].keys(), payloads["b"].keys())
        for name in payloads["a"]:
            self.assertEqual(payloads["a"][name], payloads["b"][name], name)

    def test_manifest_pins_each_artifact_hash(self):
        manifest = json.loads((OUT / "manifest.json").read_text(encoding="utf-8"))
        for name, record in manifest["artifacts"].items():
            digest = rag_index.sha256_hex((OUT / name).read_bytes())
            self.assertEqual(record["sha256"], digest, name)
            self.assertEqual(record["bytes"], (OUT / name).stat().st_size, name)

    def test_source_hash_matches_the_real_file_bytes(self):
        manifest = json.loads((OUT / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["inputs"]), 6)
        for row in manifest["inputs"]:
            raw = (ROOT / row["path"]).read_bytes()
            self.assertEqual(row["sha256"], rag_index.sha256_hex(raw), row["path"])
            self.assertEqual(row["bytes"], len(raw), row["path"])


class CorpusShape(unittest.TestCase):
    def setUp(self):
        skills, _sources, status = rag_index.collect_skills()
        self.corpus, self.index, self.manifest, self.stats, self.sections = \
            build(skills, status=status)

    def test_catalog_sections_are_the_expected_prose_fields(self):
        self.assertEqual({c["section"] for c in self.corpus["chunks"]},
                         {"description", "prompt", "starter"})

    def test_every_row_carries_the_full_contract(self):
        for chunk in self.corpus["chunks"]:
            self.assertEqual(set(chunk), CHUNK_FIELDS)
            self.assertRegex(chunk["chunk_id"], r"^sha256:[0-9a-f]{64}$")
            self.assertRegex(chunk["citation"], r"^[A-Za-z0-9_-]+#[a-z0-9]+(:\d+)?$")
            self.assertEqual(chunk["content_status"], "sample")

    def test_text_is_exactly_the_stored_span(self):
        for chunk in self.corpus["chunks"]:
            text = self.sections[(chunk["skill_id"], chunk["section"])]
            self.assertEqual(chunk["text"], text[chunk["span"][0]:chunk["span"][1]],
                             chunk["citation"])

    def test_coverage_never_drops_a_character(self):
        for (skill_id, section), text in self.sections.items():
            ranges = [c["covered"] for c in self.corpus["chunks"]
                      if c["skill_id"] == skill_id and c["section"] == section]
            merged = []
            for start, end in sorted(ranges):
                if merged and start <= merged[-1][1]:
                    merged[-1][1] = max(merged[-1][1], end)
                else:
                    merged.append([start, end])
            covered = "".join(text[s:e] for s, e in merged)
            self.assertEqual(re.sub(r"\s+", "", covered), re.sub(r"\s+", "", text),
                             f"{skill_id}#{section}")

    def test_overlap_is_never_counted_as_new_content(self):
        for chunk in self.corpus["chunks"]:
            self.assertEqual(chunk["chars"] - chunk["overlap_chars"], chunk["covered_chars"],
                             chunk["citation"])
            self.assertEqual(chunk["covered"][0] - chunk["span"][0], chunk["overlap_chars"],
                             chunk["citation"])

    def test_citations_are_unique_addressable_keys(self):
        citations = [c["citation"] for c in self.corpus["chunks"]]
        self.assertEqual(len(citations), len(set(citations)))
        by_citation = {c["citation"]: c for c in self.corpus["chunks"]}
        for row in self.index["chunks"]:
            self.assertIn(row["citation"], by_citation)
            self.assertEqual(by_citation[row["citation"]]["chunk_id"], row["chunk_id"])

    def test_postings_only_reference_indexed_rows(self):
        indexed = {c["ci"] for c in self.corpus["chunks"] if c["indexed"]}
        for term, entry in self.index["postings"].items():
            self.assertEqual(entry["df"], len(entry["p"]), term)
            for ci, tf in entry["p"]:
                self.assertIn(ci, indexed, term)
                self.assertGreaterEqual(tf, 1, term)

    def test_index_is_compact_and_corpus_is_jsonl(self):
        raw_index = (OUT / "index.json").read_text(encoding="utf-8")
        self.assertNotIn("\n  ", raw_index, "the search index must stay machine-compact")
        lines = (OUT / "corpus.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), self.index["chunk_count"])
        for line in lines:
            json.loads(line)

    def test_doc_keywords_include_name_and_tags_but_never_become_chunks(self):
        doc = self.index["docs"][0]
        self.assertIn(doc["title"], doc["keywords"])
        self.assertTrue(set(doc["tags"]) <= set(doc["keywords"]))
        self.assertNotIn("name", {c["section"] for c in self.corpus["chunks"]})

    def test_merge_warnings_and_flags_agree(self):
        self.assertEqual(self.stats["counts"]["tail_merged"], self.stats["warnings"]["tail_merges"])
        self.assertEqual(self.stats["counts"]["tail_merged"], 0,
                         "no real section is short enough to need a tail merge")

    def test_stats_counts_agree_with_the_rows(self):
        counts = self.stats["counts"]
        self.assertEqual(counts["chunks"], len(self.corpus["chunks"]))
        self.assertEqual(counts["indexed_chunks"] + counts["duplicate_chunks"],
                         counts["chunks"])
        self.assertEqual(counts["skills"], len(self.index["docs"]))
        self.assertEqual(sum(row["chunks"] for row in self.stats["per_doc"].values()),
                         counts["chunks"])

    def test_stems_expand_arabic_onto_arabic_only(self):
        for stem, forms in self.index["stem_forms"].items():
            self.assertEqual(forms, sorted(forms), stem)
            self.assertNotIn(stem, forms, f"{stem} should not list itself")
            for form in forms:
                # expansion is reversible: the stem is a contiguous core of the form
                self.assertIn(stem, form, f"{form} -> {stem}")
                self.assertGreaterEqual(len(form), len(stem) + 1, form)
        self.assertEqual(rag_index.tokenize("وRTL"), ["وrtl"])
        self.assertEqual(rag_index.stem("وrtl"), "وrtl",
                         "latin text must not be stripped as if it were Arabic")


class SyntheticGuards(unittest.TestCase):
    def test_identical_text_across_skills_is_deduped_not_indexed(self):
        twin = skill(id="SKL002", name="مرآة")
        corpus, index, _manifest, stats, _sections = build([skill(), twin])
        dupes = [c for c in corpus["chunks"] if c["duplicate_of"]]
        self.assertTrue(dupes, "cloned sections must be detected as duplicates")
        for row in dupes:
            self.assertFalse(row["indexed"])
            self.assertEqual(row["duplicate_of"], row["chunk_id"])
        indexed = {c["ci"] for c in corpus["chunks"] if c["indexed"]}
        for term, entry in index["postings"].items():
            for ci, _tf in entry["p"]:
                self.assertIn(ci, indexed, term)
        self.assertEqual(stats["counts"]["duplicate_chunks"], len(dupes))
        # the duplicate is still *covered*, so nothing is lost by pointing it elsewhere
        self.assertTrue(all(c["covered"][1] > c["covered"][0] for c in corpus["chunks"]))

    def test_oversized_block_is_split_without_losing_text(self):
        monster = "معلومة " * 900  # one block far past max_chars, no sentence marks
        corpus, _index, _manifest, _stats, sections = build([skill(prompt=monster)])
        chunks = [c for c in corpus["chunks"] if c["section"] == "prompt"]
        self.assertGreater(len(chunks), 2)
        for chunk in chunks:
            self.assertLessEqual(chunk["covered_chars"], rag_index.PIPELINE["max_chars"])
        text = sections[("SKL001", "prompt")]
        covered = "".join(text[c["covered"][0]:c["covered"][1]] for c in sorted(chunks, key=lambda c: c["covered"]))
        self.assertEqual(covered.replace(" ", ""), text.replace(" ", ""))

    def test_short_tail_merges_when_it_fits_and_stays_otherwise(self):
        # A tail under min_chars folds into the previous chunk while the result stays
        # inside max_chars; when it would not, it remains its own chunk -- it is never
        # dropped, because dropping is the failure mode this stage exists to prevent.
        fits = "أ" * 700 + " " + "ب" * 30
        chunks, _blocks, merges = rag_index.chunk_section(fits)
        self.assertEqual(len(chunks), 1, [c["covered"] for c in chunks])
        self.assertEqual(merges, 0)  # one greedy chunk, nothing to fold
        self.assertEqual(chunks[0]["covered_chars"], len(fits))
        self.assertFalse(chunks[0]["tail_merged"])
        # a tail that cannot fit under max_chars folds in with the documented tolerance
        tight = "أ" * 1600 + " " + "ب" * 30
        chunks, _blocks, merges = rag_index.chunk_section(tight)
        self.assertEqual(merges, 1, [c["covered"] for c in chunks])
        self.assertEqual(len(chunks), 1)
        self.assertTrue(chunks[0]["tail_merged"])
        self.assertEqual(chunks[0]["covered_chars"], len(tight))
        rag_index.verify(rows_from(chunks, tight, "x"), {("S", "x"): tight})
        # past the tolerance the tail stays its own chunk instead of vanishing
        original = dict(rag_index.PIPELINE)
        rag_index.PIPELINE.update({"max_chars": 1600, "min_chars": 10})
        try:
            chunks, _blocks, merges = rag_index.chunk_section(tight)
            self.assertEqual(merges, 0, "the tolerance must be enforced, not ignored")
            self.assertEqual(len(chunks), 2)
        finally:
            rag_index.PIPELINE.clear()
            rag_index.PIPELINE.update(original)
        self.assertEqual(len(chunks), 2, [c["covered"] for c in chunks])
        self.assertEqual(chunks[0]["covered_chars"], 1600)
        self.assertEqual(chunks[1]["covered_chars"], 30)
        self.assertTrue(all(chunk["span"][1] - chunk["span"][0] == chunk["chars"]
                            for chunk in chunks))

    def test_editing_one_section_leaves_another_identity_untouched(self):
        before = build([skill()])[0]["chunks"]
        after = build([skill(starter="مثال مختلف تمامًا.")])[0]["chunks"]
        self.assertEqual({c["chunk_id"] for c in before if c["section"] == "description"},
                         {c["chunk_id"] for c in after if c["section"] == "description"})
        self.assertNotEqual({c["chunk_id"] for c in before if c["section"] == "starter"},
                            {c["chunk_id"] for c in after if c["section"] == "starter"})

    def test_diacritics_survive_in_evidence_but_fold_for_search(self):
        text = "بِسْمِ اللهِ الرَّحْمٰنِ الرَّحِيمِ"
        corpus = build([skill(description=text)])[0]["chunks"]
        self.assertIn("ِ", corpus[0]["text"], "stored evidence must keep the source as-is")
        self.assertEqual(rag_index.content_terms(text), rag_index.content_terms("بسم الله الرحمن الرحيم"))

    def test_unindexed_prose_is_reported_not_swallowed(self):
        long_extra = ("هذه فقرة نثرية إضافية طويلة بما يكفي لتلفت الانتباه، وهي لا تُفهرس "
                      "بصمت داخل عقد البيانات الحالي لذلك يجب أن تظهر في التحذيرات")
        self.assertGreater(len(long_extra), 80)
        stats = build([skill(extra_note=long_extra)])[3]
        self.assertIn("SKL001.extra_note", stats["warnings"]["unindexed_prose"])
        short = build([skill(extra_note="قصير")])[3]
        self.assertEqual(short["warnings"]["unindexed_prose"], [])

    def test_sections_list_is_supported_for_the_future_catalog(self):
        corpus, _index, _manifest, _stats, _sections = build(
            [skill(sections=[{"title": "الخطوات", "body": "اكتب المسودة أولًا."},
                             {"title": "الأخطاء", "body": "لا تخترع معلومات."}])])
        self.assertEqual([c["section"] for c in corpus["chunks"] if c["section"].startswith("section")],
                         ["section1", "section2"])
        self.assertTrue(all(c["citation"].count("#") == 1 for c in corpus["chunks"]))


class VectorGate(unittest.TestCase):
    """The R5 decision must be computed from measurements, never from a meeting."""

    def test_sample_catalog_stays_lexical(self):
        gate = json.loads((OUT / "stats.json").read_text(encoding="utf-8"))["vector_gate"]
        self.assertFalse(gate["vector_enabled"])
        self.assertEqual(gate["decision"], "lexical-only")
        self.assertEqual(gate["reasons"], [])
        self.assertIsNone(gate["measured"]["lexical_recall_at5"])

    def test_gate_flips_when_the_corpus_grows(self):
        gate = build([skill(description="كلمة " * 60000)])[3]["vector_gate"]
        self.assertTrue(gate["vector_enabled"])
        self.assertIn("corpus_bytes", " ".join(gate["reasons"]))

    def test_gate_flips_on_measured_recall(self):
        _c, _i, _m, stats, _s = build([skill()], recall=0.62)
        self.assertTrue(stats["vector_gate"]["vector_enabled"])
        self.assertIn("lexical_recall_at5=0.62", stats["vector_gate"]["reasons"][0])
        _c, _i, _m, ok, _s = build([skill()], recall=0.95)
        self.assertFalse(ok["vector_gate"]["vector_enabled"])

    def test_eval_report_on_disk_feeds_the_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "eval-report.json").write_text(
                json.dumps({"metrics": {"recall_at5": 0.45}}), encoding="utf-8")
            self.assertEqual(rag_index.read_eval_recall(directory), 0.45)
            (directory / "eval-report.json").write_text("{ not json", encoding="utf-8")
            self.assertIsNone(rag_index.read_eval_recall(directory))
            (directory / "eval-report.json").write_text(
                json.dumps({"metrics": {"other": 1}}), encoding="utf-8")
            self.assertIsNone(rag_index.read_eval_recall(directory))


class Guardrails(unittest.TestCase):
    def test_stale_published_catalog_is_refused(self):
        original = rag_index.load_skills
        rag_index.load_skills = lambda: [skill(description="نص معدّل لا يطابق المنشور")]
        try:
            code, output = quiet(rag_index.main, ["--check"])
            self.assertEqual(code, 1)
            self.assertIn("stale", output)
        finally:
            rag_index.load_skills = original

    def test_verify_rejects_a_corrupt_chunk(self):
        corpus, _index, _manifest, _stats, sections = build([skill()])
        corpus["chunks"][0]["text"] = "بلا سند"
        with self.assertRaises(rag_index.RagError):
            rag_index.verify(corpus["chunks"], sections)

    def test_verify_rejects_dropped_coverage(self):
        corpus, _index, _manifest, _stats, sections = build(
            [skill(prompt="هذه جملة مفيدة أخرى. " * 120)])
        rows = [c for c in corpus["chunks"] if c["section"] == "prompt"]
        self.assertGreater(len(rows), 1)
        rag_index.verify(corpus["chunks"], sections)  # control: the same rows are fine
        corpus["chunks"] = [c for c in corpus["chunks"] if c is not rows[-1]]
        with self.assertRaises(rag_index.RagError):
            rag_index.verify(corpus["chunks"], sections)

    def test_verify_rejects_a_mismatching_span(self):
        corpus, _index, _manifest, _stats, sections = build([skill()])
        corpus["chunks"][0]["span"] = [0, 1]
        with self.assertRaises(rag_index.RagError):
            rag_index.verify(corpus["chunks"], sections)

    def test_empty_corpus_is_not_an_error(self):
        rag_index.verify([], {})


if __name__ == "__main__":
    unittest.main()
