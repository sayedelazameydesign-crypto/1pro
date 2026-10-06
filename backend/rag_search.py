"""R2: read-only lexical retrieval over the R1 index. Nothing else.

This module is the ONLY consumer of `data/rag/`. It reads the committed artifacts
exactly as `scripts/rag_index.py` wrote them — no re-chunking, no re-hashing, no
second copy of the text, no parallel schema — and answers a query with ranked chunks
whose `citation` and `chunk_id` are R1's own strings.

Why it borrows its tokenizer instead of owning one: the postings in `index.json` were
built with `backend/rag_text.py`'s `content_terms`. Any local re-implementation of the
folding rules would silently mismatch them and quietly degrade ranking, which no test
would catch -- so the shared module is imported, and R1 imports the same one. If the
index disagrees with us about the tokenizer version, the endpoint refuses to answer
(503) rather than scoring words into a different language.

Deliberate limits, in this stage:

* BM25 (k1=1.4, b=0.75) over R1's postings. Section weights are applied *here*, at
  query time, so `starter` text never masquerades as stronger evidence than a
  `prompt`: measured, 6 of 9 probe queries returned a `starter` chunk first.
* Conversational phrases (`اشرح لي`, `ما الفرق بين`, `ساعدني`…) are stripped from the
  QUERY only. R1's stored evidence is untouched — deleting those words from the
  corpus would have been rewriting the source to flatter the ranker.
* Stem expansion is a bonus signal at EXPANSION_WEIGHT, never a replacement, because
  R1 keeps surface terms as canonical.
* `coverage` counts DIRECT surface matches only. A hit reached solely through R1's
  expansion dict scores below and reports coverage 0.0 on purpose: "we inferred this"
  must stay visibly weaker than "the corpus says this".

* NO ANSWER THRESHOLD. "Is this answerable?" is a measured question; R4 decides it and
  writes `eval-report.json`. Callers get `score`, `explain`, `coverage` and
  `no_answer.decision="deferred"` — the inputs for that judgement, not a guess.
* No timestamps, no timings, no random ordering, so two calls return byte-identical
  JSON. (Elapsed ms would have made every snapshot test lie eventually.)
* No DB write and no rate-limit row: a search is a 20 KB file read with no model call,
  and burning Neon compute-hours on it while the chat budget counts the same table
  would be a self-inflicted outage. The DoS ceiling is instead `|q|<=400` and `k<=20`.
"""
import json
import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEARCH_VERSION = "waha.rag-search/1"
RAG_MODE = "RAG_LOCAL"
DEFAULT_INDEX_DIR = ROOT / "data/rag"
BM25_K1 = 1.4
BM25_B = 0.75
MAX_QUERY_CHARS = 400
MAX_RESULTS = 20
DEFAULT_RESULTS = 5
SNIPPET_CHARS = 400
# Weight per section: retrieval must rank evidence, not example prompts. Unknown
# sections (the future `section1`/`lesson2`) keep neutral 1.0.
SECTION_WEIGHTS = {"description": 1.0, "prompt": 1.15, "starter": 0.55}
DEFAULT_SECTION_WEIGHT = 1.0
EXPANSION_WEIGHT = 0.45
# Query-side phatic wrappers measured against R1's `starter` chunks (see
# RAG-FREE-PLAN.md §3.1). Multi-word first, so a topical verb survives on its own.
PHRASE_PATTERNS = (
    r"ا\s*شرح\s+لي", r"ا\s*شرح\s+لي\s+الفرق\s+بين", r"عرِّ?ف\s+لي", r"وضِّ?ح\s+لي",
    r"قل\s+لي", r"احكِ?\s*لي", r"ساعدني\s+في", r"ساعدني", r"أَ?ريد\s+أ\s*ن", r"أَ?ريد",
    r"أَ?بغى", r"عايز", r"بحاجة\s+إلى", r"أَ?حتاج\s+إلى", r"من\s+فضلك", r"لو\s+سمحت",
    r"ببساطة", r"باختصار", r"هل\s+يمكن", r"كيف\s+يمكنني", r"كيف\s+يمكن",
    r"ما\s+هو\s+الفرق\s+بين", r"ما\s+هي\s+الفرق\s+بين", r"ما\s+الفرق\s+بين",
    r"الفرق\s+بين", r"ما\s+هي", r"ما\s+هو", r"ممكن\s+لي", r"ممكن",
)
PHRASE_RE = tuple(re.compile(pattern) for pattern in PHRASE_PATTERNS)


class RagSearchUnavailable(RuntimeError):
    """Index missing, unreadable, or built by an incompatible pipeline."""


# --------------------------------------------------------------------------- folding
from rag_text import TOKENIZER, content_terms, tokenize as rag_text_tokenize  # noqa: E402

EXPECTED_FORMAT = "waha.rag.v1"        # must equal rag_index.FORMAT; pinned by a test


# -------------------------------------------------------------------------- index
_CACHE = {}


def _signature(paths):
    return tuple((path.name, path.stat().st_mtime_ns, path.stat().st_size) for path in paths)


def load(index_dir=None):
    """Return the parsed R1 artifacts, re-read only when the files actually change."""
    directory = Path(index_dir) if index_dir else DEFAULT_INDEX_DIR
    files = {name: directory / name for name in ("index.json", "corpus.jsonl",
                                                 "manifest.json", "stats.json")}
    if not files["index.json"].exists():
        raise RagSearchUnavailable(f"no R1 index at {directory}; run scripts/rag_index.py")
    signature = _signature(tuple(files.values()))
    cached = _CACHE.get(str(directory))
    if cached and cached[0] == signature:
        return cached[1]
    try:
        index = json.loads(files["index.json"].read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise RagSearchUnavailable(f"unreadable index.json: {error}")
    if index.get("format") != EXPECTED_FORMAT:
        raise RagSearchUnavailable(
            f"index format {index.get('format')!r} != {EXPECTED_FORMAT!r}; rebuild data/rag")
    pipeline = index.get("pipeline", {})
    if pipeline.get("tokenizer") != TOKENIZER:
        raise RagSearchUnavailable(
            f"index was built with tokenizer {pipeline.get('tokenizer')!r}, this query "
            f"path folds with {TOKENIZER!r}; rebuild data/rag")
    rows = []
    if files["corpus.jsonl"].exists():
        for line in files["corpus.jsonl"].read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    if len(rows) != index.get("chunk_count"):
        raise RagSearchUnavailable(
            f"corpus.jsonl has {len(rows)} rows, index.json claims {index.get('chunk_count')}")
    texts = {row["ci"]: row["text"] for row in rows}
    meta = {}
    for name, key in (("stats.json", "stats"), ("manifest.json", "manifest")):
        if files[name].exists():
            try:
                meta[key] = json.loads(files[name].read_text(encoding="utf-8"))
            except (OSError, ValueError):
                meta[key] = {}
    data = {"index": index, "rows": {row["ci"]: row for row in rows}, "texts": texts,
            "signature": signature, "directory": str(directory), **meta}
    _CACHE[str(directory)] = (signature, data)
    return data


def status(index_dir=None):
    """Small, honest capability block for /health and /api/config."""
    try:
        data = load(index_dir)
    except (RagSearchUnavailable, OSError) as error:
        return {"ready": False, "mode": None, "reason": str(error),
                "search_version": SEARCH_VERSION}
    stats = data.get("stats") or {}
    counts = stats.get("counts") or {}
    gate = stats.get("vector_gate") or {}
    return {"ready": True, "mode": "lexical", "search_version": SEARCH_VERSION,
            "chunks": counts.get("chunks", data["index"]["chunk_count"]),
            "indexed_chunks": counts.get("indexed_chunks"),
            "vocab": counts.get("vocab", data["index"]["vocab"]),
            "content_status": data["index"].get("content_status"),
            "vector_gate": gate.get("decision"),
            "reason": None}


# ------------------------------------------------------------------------ scoring
def clean_query(query):
    """Fold, drop phatic wrappers, keep content terms. Returns bookkeeping too."""
    raw = " ".join(str(query or "").split())
    removed = []
    text = raw
    for pattern in PHRASE_RE:
        replaced = pattern.sub(" ", text)
        if replaced != text:
            removed.append(pattern.pattern)
            text = replaced
    raw_terms = rag_text_tokenize(raw)
    terms = content_terms(text)
    return {"raw": raw, "text": re.sub(r"\s+", " ", text).strip(), "raw_terms": raw_terms,
            "terms": terms, "removed_phrases": list(dict.fromkeys(removed))}


def _term_weights(terms, index):
    """Query terms weigh 1.0; anything *inferred* weighs less and never displaces a
    direct surface match. Two inferences are allowed, and both come from artifacts R1
    already committed -- no new lexicon, no transliteration table:

    * `stem_forms` (stem -> the surface forms that stemmed to it), which R1 publishes
      for exactly this purpose: `جمهور` reaches the indexed `الجمهور`.
    * the definite article only: `الوقت` has no posting while `وقت` does, and `ال` is a
      function word rather than part of the lexical stem.

    The full affix-lite stem is deliberately NOT run over queries, on measurement: with
    it, `اليوم` folded to `يوم` and then reached the forms `يوميه`/`يومين` through
    `stem_forms`, which made "what is the Lira exchange rate" hit a time-management
    skill. One prefix strip adds the recall that matters and cannot invent that junk.
    Anything inferred is reported in `inferred_terms`, `matched_weight` in each row is
    the strongest weight behind the hit (so `1.0` means "direct", `< 1.0` means
    "inferred"), and `coverage` counts direct matches only: an inferred hit must never
    dress up as a direct one. The Latin gap (`بايثون` vs the indexed `python`) stays
    unreachable on purpose -- transliteration is R4's measured decision, not a scorer hack.
    """
    forms = index.get("stem_forms") or {}
    weights, inferred = {}, set()

    def offer(term, weight):
        if weights.get(term, 0.0) >= weight:
            return
        weights[term] = weight
        inferred.add(term)

    for term in terms:
        weights[term] = 1.0                                     # the surface form is canonical
        inferred.discard(term)
        for extra in forms.get(term, ()):
            offer(extra, EXPANSION_WEIGHT)
        if term.startswith("ال") and len(term) > 4:
            # The bare form only -- and deliberately NOT its own expansion set. Running
            # `stem_forms` on the stripped form reintroduced measured junk: `اليوم` ->
            # `يوم` -> `يوميه/يومين`, which scored a foreign-currency question onto a
            # time-management skill. Recall holes are cheaper than invented relevance.
            offer(term[2:], EXPANSION_WEIGHT)
    return weights, inferred


def search(query, k=DEFAULT_RESULTS, sections=None, skills=None, index_dir=None):
    """Ranked chunks plus the metadata R4 needs to decide 'answered' or not."""
    data = load(index_dir)
    index = data["index"]
    limit = max(1, min(int(k or DEFAULT_RESULTS), MAX_RESULTS))
    wanted_sections = {value for value in (sections or []) if value}
    wanted_skills = {value for value in (skills or []) if value}
    cleaned = clean_query(query)
    postings = index.get("postings") or {}
    lengths = index.get("chunk_len") or []
    doc_count = len(lengths)
    average = (index.get("total_len") or 0) / doc_count if doc_count else 0.0
    weights, inferred = _term_weights(cleaned["terms"], index)
    rows = data["rows"]
    # The section weight belongs INSIDE the accumulation: discounting after sorting
    # would leave `starter` on top and only print a smaller number, which is exactly
    # the failure this stage exists to fix.
    scores, raw_scores, matched, explanations = {}, {}, set(), {}
    if average:
        for term, weight in sorted(weights.items()):
            entry = postings.get(term)
            if not entry:
                continue
            matched.add(term)
            idf = math.log(1 + (doc_count - entry["df"] + 0.5) / (entry["df"] + 0.5))
            for ci, tf in entry["p"]:
                length = lengths[ci] or 1
                denominator = tf + BM25_K1 * (1 - BM25_B + BM25_B * length / average)
                contribution = weight * idf * (tf * (BM25_K1 + 1)) / denominator
                section_weight = SECTION_WEIGHTS.get(rows[ci]["section"], DEFAULT_SECTION_WEIGHT)
                scores[ci] = scores.get(ci, 0.0) + contribution * section_weight
                raw_scores[ci] = raw_scores.get(ci, 0.0) + contribution
                row = explanations.setdefault(ci, {"matched": {}, "idf_sum": 0.0})
                row["matched"][term] = row["matched"].get(term, 0) + tf
                row["idf_sum"] += weight * idf
    results = []
    for ci in sorted(scores, key=lambda key: (-round(scores[key], 12), key)):
        row = rows[ci]
        if wanted_sections and row["section"] not in wanted_sections:
            continue
        if wanted_skills and row["skill_id"] not in wanted_skills:
            continue
        section_weight = SECTION_WEIGHTS.get(row["section"], DEFAULT_SECTION_WEIGHT)
        explain = explanations[ci]
        results.append({
            "ci": ci,
            "chunk_id": row["chunk_id"],
            "citation": row["citation"],
            "skill_id": row["skill_id"],
            "section": row["section"],
            "section_label": row["section_label"],
            "ordinal": row["ordinal"],
            "title": row["title"],
            "snippet": row["text"][:SNIPPET_CHARS] + ("…" if len(row["text"]) > SNIPPET_CHARS else ""),
            "score": round(scores[ci], 6),
            "explain": {"matched": {term: count for term, count in sorted(explain["matched"].items())},
                        "matched_weight": round(max((weights.get(term, 0.0) for term in explain["matched"]),
                                                    default=0.0), 4),
                        "section_weight": section_weight,
                        "unweighted_score": round(raw_scores[ci], 6),
                        "doc_len": lengths[ci], "covered_chars": row["covered_chars"]},
            "source": row["source"],
            "source_hash": row["source_hash"],
            "content_status": row["content_status"],
            "rag": True,
        })
        if len(results) >= limit:
            break
    content_terms = len(cleaned["terms"])
    direct = {term for term in matched if weights.get(term) == 1.0}
    unmatched = sorted({term for term in cleaned["terms"] if term not in matched})
    return {
        "query": cleaned["raw"],
        "normalized_query": cleaned["text"],
        "search_mode": "lexical",
        "evidence": RAG_MODE,
        "results": results,
        "query_stats": {
            "raw_term_count": len(cleaned["raw_terms"]),
            "content_term_count": content_terms,
            "matched_terms": sorted(matched),
            "unmatched_terms": unmatched,
            "inferred_terms": sorted(inferred & matched),
            "removed_phrases": cleaned["removed_phrases"],
            "coverage": round(len(direct) / content_terms, 4) if content_terms else 0.0,
        },
        "no_answer": {
            "decision": "deferred" if content_terms else "unsearchable",
            "policy": ("no threshold in R2: R4 measures and writes data/rag/eval-report.json"
                       if content_terms else
                       "the query held no content terms after phatic phrases were removed"),
            "max_score": results[0]["score"] if results else 0.0,
            "result_count": len(results),
        },
        "params": {"k1": BM25_K1, "b": BM25_B, "k": limit,
                   "sections": sorted(wanted_sections) or None,
                   "skills": sorted(wanted_skills) or None},
        "weights": {"section": dict(SECTION_WEIGHTS), "section_default": DEFAULT_SECTION_WEIGHT,
                    "expansion": EXPANSION_WEIGHT, "phrase_patterns": len(PHRASE_RE)},
        "index": {"format": index["format"], "pipeline": index["pipeline"],
                  "chunks": index["chunk_count"], "docs": index["doc_count"],
                  "vocab": index["vocab"], "content_status": index.get("content_status"),
                  "directory": data["directory"],
                  "manifest_sha256": (data.get("manifest") or {}).get("artifacts", {})
                  .get("index.json", {}).get("sha256"),
                  "vector_gate": ((data.get("stats") or {}).get("vector_gate") or {})
                  .get("decision")},
        "search_version": SEARCH_VERSION,
    }
