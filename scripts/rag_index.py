"""Build a deterministic, provenance-complete RAG corpus and lexical index.

R1 of the free RAG plan (see RAG-FREE-PLAN.md). This stage deliberately needs no
API, no GPU, no vector database and no third-party package: it turns the canonical
catalog into an evidence-complete corpus, so that R2 (search), R3 (the agent's
``kb_search`` tool) and R4 (evaluation) all read ONE schema instead of inventing
parallel ones.

    skills/*/skill.json   (loaded by build_catalog.load_skills -- same validator)
      -> canonical sections       (prose fields; sections[]/lessons[] when added)
      -> normalized text          (NFKC + line hygiene; diacritics KEPT)
      -> blocks                   (paragraph -> sentence -> hard split if oversized)
      -> chunks                   (greedy pack <= max_chars, whole-block overlap)
      -> SHA-256 dedup            (duplicate text points at the canonical chunk)
      -> lexical index            (BM25-ready postings + reversible stem expansion)

Guarantees, asserted by --verify / self-test / tests/test_rag_index.py:

1. DETERMINISTIC -- same sources + same pipeline => byte-identical output. Nothing
   wall-clock is written, so CI can diff the committed artifacts forever.
2. LOSSLESS -- every chunk stores ``span`` [start,end) offsets into its normalized
   section text and ``text == normalized[start:end]``; ``covered`` is the part of
   that span no earlier chunk already carried. Covering ranges tile the section, so
   chunking may repeat text but may never drop it.
3. PROVENANCE -- every chunk carries source path, ``source_hash`` (file bytes),
   ``chunk_hash`` (its own text), section, ordinal and a resolvable ``citation``
   ("SKL001#prompt", or "SKL001#prompt:2" when a section split). An answer either
   cites one of those or does not claim a source.
4. IDENTITY BY CONTENT -- chunk_id = sha256(FORMAT, skill_id, section, chunk_hash).
   Re-chunking around unchanged text keeps citations stable; editing the text
   changes the identity. Position is metadata (``ordinal``), not identity.
5. THE SIZE GATE IS DATA -- stats.json computes when embeddings (R5) become
   justified, and reads R4's ``eval-report.json`` for the recall condition. "Should
   we vector-embed?" stays a computed answer, not a feeling.

Refused here: embeddings, NVIDIA/cloud calls, HTTP, third-party imports, timestamps,
random ordering, and any second loader for skill content.
"""
import argparse
import hashlib
import json
import re
import sys
import unicodedata
from pathlib import Path

# The catalog loader/validator is imported, never copied: one schema decides what a
# skill is, so this stage cannot drift from what Pages and the backend publish.
sys.path.insert(0, str(Path(__file__).resolve().parent))
# The shared module sits in backend/ one directory up; resolve it independently of
# the working directory, exactly as the Vercel wrapper does for the app itself.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from build_catalog import ROOT, json_bytes, load_skills  # noqa: E402
# The shared folding rules are imported, not copied: R2 queries with these exact
# functions, so postings and query keys can never drift apart. It lives in backend/
# because Vercel ships that directory and excludes scripts/. The names below stay
# re-exported here for the pipeline stages and the tests that call them through R1.
from rag_text import (PARAGRAPH_SPLIT, SENTENCE_SPLIT, STOPWORDS, STEMMER, TOKENIZER,
                      content_terms, normalize_search, normalize_text, stem, tokenize)

FORMAT = "waha.rag.v1"
PIPELINE = {
    "chunker": "block-greedy-overlap/1",
    "tokenizer": TOKENIZER,
    "stemmer": STEMMER,
    "max_chars": 1600,
    "min_chars": 120,
    "overlap_ratio": 0.15,
    "overlap_max_chars": 240,
    "hard_split_floor": 0.6,
    "max_stem_forms": 32,
}
# Fixed order, and a fixed set: prose fields become sections. Short metadata
# (name/category/difficulty/tags) is NOT a chunk -- a chunk that answers nothing
# only dilutes recall -- it is indexed as doc keywords for R2 to OR in.
SECTION_FIELDS = (("description", "description", "الوصف"),
                  ("prompt", "prompt", "الإرشاد التعليمي"),
                  ("starter", "starter", "مثال بداية"))
LIST_SECTIONS = (("sections", "section", ("body",), "قسم"),
                 ("lessons", "lesson", ("content", "body", "text"), "درس"))
METADATA_FIELDS = ("id", "name", "category", "difficulty", "icon", "color", "tags")
DOC_KEYWORD_FIELDS = ("name", "category", "difficulty")
GATE_THRESHOLDS = {"corpus_bytes": 200_000, "skill_count": 50, "pdf_count": 1,
                   "lexical_recall_at5": 0.80}
class RagError(ValueError):
    """A corpus contract was violated; refuse to write a partial index."""


def sha256_hex(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


# Text folding lives in backend/rag_text.py (see the import above): indexing here and
# querying in backend/rag_search.py must never disagree about what a word is.



# ------------------------------------------------------------------- section model

def section_sources(skill):
    """Yield (section, label, text) in a fixed, versioned order."""
    for field, section, label in SECTION_FIELDS:
        value = skill.get(field)
        if isinstance(value, str) and value.strip():
            yield section, label, value
    for key, prefix, body_keys, label in LIST_SECTIONS:
        items = skill.get(key)
        if not isinstance(items, list):
            continue
        for number, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                continue
            for body_key in body_keys:
                value = item.get(body_key)
                if isinstance(value, str) and value.strip():
                    yield f"{prefix}{number}", f"{label} {number}", value
                    break


def unindexed_prose(skill):
    """Prose-looking string fields the contract does not index: a warning, never a
    silent drop, so a future schema change trips here rather than losing recall."""
    known = {field for field, _, _ in SECTION_FIELDS} | set(METADATA_FIELDS)
    extra = {key for _, key, _, _ in LIST_SECTIONS}
    return sorted(key for key, value in skill.items()
                  if isinstance(value, str) and len(value) > 80
                  and key not in known and key not in extra)


def doc_keywords(skill):
    terms = []
    for field in DOC_KEYWORD_FIELDS:
        value = skill.get(field)
        if isinstance(value, str) and value.strip():
            terms.append(value.strip())
    for value in skill.get("tags") or []:
        if isinstance(value, str) and value.strip():
            terms.append(value.strip())
    return list(dict.fromkeys(terms))


# ------------------------------------------------------------------------ chunking

def split_blocks(text, max_chars, floor_ratio):
    """Paragraph -> sentence -> hard split, each block as exact [start, end)."""
    blocks = []
    cursor = 0
    ends = [match.start() for match in PARAGRAPH_SPLIT.finditer(text)] + [len(text)]
    for end in ends:
        blocks.extend(_sentence_blocks(text[cursor:end], cursor, max_chars, floor_ratio))
        cursor = end
    return [block for block in blocks if text[block[0]:block[1]].strip()]


def _sentence_blocks(text, offset, max_chars, floor_ratio):
    if not text.strip():
        return []
    blocks, cursor = [], 0
    ends = [match.start() for match in SENTENCE_SPLIT.finditer(text)] + [len(text)]
    for end in ends:
        blocks.extend(_hard_split(text[cursor:end], offset + cursor, max_chars, floor_ratio))
        cursor = end
    return blocks


def _hard_split(text, start, max_chars, floor_ratio):
    """Oversized single blocks break at the last space past the floor, else hard."""
    pieces, cursor = [], 0
    while len(text) - cursor > max_chars:
        window = text[cursor:cursor + max_chars]
        room = window.rfind(" ")
        cut = room if room >= int(max_chars * floor_ratio) else max_chars
        pieces.append((start + cursor, start + cursor + cut))
        cursor += cut
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
    if len(text) - cursor > 0:
        pieces.append((start + cursor, start + len(text)))
    return pieces


def chunk_section(text):
    """Return ([{start,end,span_from,span_to,overlap_blocks,overlap_chars}], blocks)."""
    max_chars = PIPELINE["max_chars"]
    blocks = split_blocks(text, max_chars, PIPELINE["hard_split_floor"])
    if not blocks:
        return [], blocks
    budget = min(PIPELINE["overlap_max_chars"],
                 int(max_chars * PIPELINE["overlap_ratio"]))
    raw, index = [], 0
    while index < len(blocks):
        end, size = index, 0
        while end < len(blocks):
            length = blocks[end][1] - blocks[end][0]
            if end > index and size + length > max_chars:
                break
            size += length
            end += 1
            if size >= max_chars:
                break
        start = index
        previous = raw[-1] if raw else None
        if previous:
            # Overlap may only eat the previous chunk's *newly added* blocks, and must
            # leave at least one of them behind -- otherwise a chunk duplicates its
            # predecessor entirely and contributes nothing to coverage.
            low = previous["start"] + previous["overlap_blocks"] + 1
            used = 0
            while start - 1 >= low:
                extra = blocks[start - 1][1] - blocks[start - 1][0]
                if used + extra > budget:
                    break
                used += extra
                start -= 1
        raw.append({"start": start, "end": end, "overlap_blocks": index - start,
                    "overlap_chars": blocks[index][0] - blocks[start][0] if start < index else 0})
        index = end
    chunks, merges = [], 0

    for item in raw:
        span = [blocks[item["start"]][0], blocks[item["end"] - 1][1]]
        covered = [blocks[item["start"] + item["overlap_blocks"]][0], span[1]]
        record = dict(item, span=span, covered=covered, chars=span[1] - span[0],
                      covered_chars=covered[1] - covered[0], tail_merged=False)
        # A tail shorter than min_chars is folded into the previous chunk. A greedy
        # packer can only leave such a tail when the previous chunk is already at the
        # cap, so the fold is allowed a documented tolerance of one min_chars: an
        # oversized-but-flagged chunk retrieves far better than a 20-character
        # fragment, and dropping the tail is never an option. Beyond the tolerance the
        # tail stays visible as its own chunk.
        if (chunks and record["covered_chars"] < PIPELINE["min_chars"]
                and span[1] - chunks[-1]["covered"][0]
                <= max_chars + PIPELINE["min_chars"]):
            tail = chunks[-1]
            tail["end"] = item["end"]
            tail["span"][1] = span[1]
            tail["covered"][1] = covered[1]
            tail["chars"] = span[1] - tail["span"][0]
            tail["covered_chars"] = covered[1] - tail["covered"][0]
            tail["tail_merged"] = True
            merges += 1
            continue
        chunks.append(record)
    return chunks, blocks, merges


# --------------------------------------------------------------------------- build

def build(skills, sources, content_status, eval_recall=None):
    """Return (corpus, index, manifest, stats, sections)."""
    chunks, docs, sections, warnings = [], [], {}, {"unindexed_prose": [], "tail_merges": 0}
    by_hash, duplicates, ci = {}, 0, 0
    for skill in skills:
        skill_id = skill["id"]
        source = sources[skill_id]
        source_hash = source["sha256"]
        title = skill.get("name") or skill_id
        first, last = ci, ci
        for prose in unindexed_prose(skill):
            warnings["unindexed_prose"].append(f"{skill_id}.{prose}")
        for section, label, raw_text in section_sources(skill):
            text = normalize_text(raw_text)
            if not text:
                continue
            sections[(skill_id, section)] = text
            items, _blocks, merges = chunk_section(text)
            for ordinal, item in enumerate(items, start=1):
                chunk_text = text[item["span"][0]:item["span"][1]]
                chunk_hash = sha256_hex(chunk_text.encode("utf-8"))
                duplicate_of = by_hash.get(chunk_hash)
                if duplicate_of is None:
                    by_hash[chunk_hash] = _chunk_id(skill_id, section, chunk_hash)
                else:
                    duplicates += 1
                canonical = duplicate_of if duplicate_of is not None else by_hash[chunk_hash]
                chunks.append({
                    "ci": ci,
                    "chunk_id": canonical,
                    "chunk_hash": chunk_hash,
                    "source_hash": source_hash,
                    "skill_id": skill_id,
                    "section": section,
                    "section_label": label,
                    "ordinal": ordinal,
                    "title": title,
                    "text": chunk_text,
                    "span": item["span"],
                    "covered": item["covered"],
                    "overlap_blocks": item["overlap_blocks"],
                    "overlap_chars": item["overlap_chars"],
                    "tail_merged": item["tail_merged"],
                    "chars": item["chars"],
                    "covered_chars": item["covered_chars"],
                    "tokens": len(content_terms(chunk_text)),
                    "citation": None,
                    "indexed": duplicate_of is None,
                    "duplicate_of": duplicate_of,
                    "source": source["path"],
                    "content_status": content_status,
                })
                last = ci + 1
                ci += 1
            warnings["tail_merges"] += merges
        docs.append({"skill_id": skill_id, "title": title, "category": skill.get("category"),
                     "difficulty": skill.get("difficulty"), "tags": list(skill.get("tags") or []),
                     "keywords": doc_keywords(skill), "source": source["path"],
                     "source_hash": source_hash, "chunk_range": [first, last]})
    _fill_citations(chunks)
    postings, chunk_len, stem_forms = _index_chunks(chunks)
    index = {"format": FORMAT, "pipeline": PIPELINE, "content_status": content_status,
             "docs": [{key: doc[key] for key in ("skill_id", "title", "category", "difficulty",
                                                 "tags", "keywords", "source", "source_hash",
                                                 "chunk_range")} for doc in docs],
             "chunks": [{key: chunk[key] for key in ("ci", "chunk_id", "chunk_hash", "skill_id",
                                                     "section", "ordinal", "title", "citation",
                                                     "indexed", "span", "covered", "chars",
                                                     "covered_chars", "tokens", "tail_merged")}
                      for chunk in chunks],
             "postings": postings, "stem_forms": stem_forms, "chunk_len": chunk_len,
             "doc_count": len(docs), "chunk_count": len(chunks),
             "vocab": len(postings), "total_len": sum(chunk_len)}
    corpus = {"format": FORMAT, "pipeline": PIPELINE, "content_status": content_status,
              "docs": docs, "chunks": chunks}
    stats = _stats(docs, chunks, duplicates, warnings, index, sections, eval_recall)
    manifest = _manifest(sources, docs, chunks, content_status)
    verify(chunks, sections)
    return corpus, index, manifest, stats, sections


def _chunk_id(skill_id, section, chunk_hash):
    return sha256_hex(f"{FORMAT}\0{skill_id}\0{section}\0{chunk_hash}".encode("utf-8"))


def _fill_citations(chunks):
    totals, seen = {}, {}
    for chunk in chunks:
        key = (chunk["skill_id"], chunk["section"])
        totals[key] = totals.get(key, 0) + 1
    for chunk in chunks:
        key = (chunk["skill_id"], chunk["section"])
        seen[key] = seen.get(key, 0) + 1
        base = f"{key[0]}#{key[1]}"
        chunk["citation"] = base if totals[key] == 1 else f"{base}:{seen[key]}"


def _index_chunks(chunks):
    postings, chunk_len, forms = {}, [], {}
    cap = PIPELINE["max_stem_forms"]
    for chunk in chunks:
        all_terms = tokenize(chunk["text"])
        chunk_len.append(len(all_terms))
        if not chunk["indexed"]:
            continue
        counts = {}
        for term in all_terms:
            if term in STOPWORDS or len(term) < 2:
                continue
            counts[term] = counts.get(term, 0) + 1
        for term, tf in counts.items():
            postings.setdefault(term, {"df": 0, "p": []})["p"].append([chunk["ci"], tf])
        for term in counts:
            key = stem(term)
            if key != term:
                bucket = forms.setdefault(key, [])
                if term not in bucket:
                    bucket.append(term)
    for entry in postings.values():
        entry["df"] = len(entry["p"])
    for key in forms:
        forms[key] = sorted(forms[key])[:cap]
    return ({key: postings[key] for key in sorted(postings)}, chunk_len,
            {key: forms[key] for key in sorted(forms)})


def _stats(docs, chunks, duplicates, warnings, index, sections, eval_recall):
    lengths = sorted(chunk["covered_chars"] for chunk in chunks) or [0]
    tokens = sorted(chunk["tokens"] for chunk in chunks) or [0]
    per_doc = {doc["skill_id"]: {"title": doc["title"], "chunks": 0, "chars": 0,
                                 "tokens": 0, "sections": 0} for doc in docs}
    corpus_bytes = 0
    for chunk in chunks:
        row = per_doc[chunk["skill_id"]]
        row["chunks"] += 1
        row["chars"] += chunk["covered_chars"]
        row["tokens"] += chunk["tokens"]
        body = sections[(chunk["skill_id"], chunk["section"])][chunk["covered"][0]:chunk["covered"][1]]
        corpus_bytes += len(body.encode("utf-8"))
    for (skill_id, _section) in sections:
        per_doc[skill_id]["sections"] += 1
    corpus_chars = sum(row["chars"] for row in per_doc.values())

    def percentile(values, part):
        return values[min(len(values) - 1, max(0, int(round(part * (len(values) - 1)))))]

    return {
        "format": FORMAT, "pipeline": PIPELINE, "content_status": index["content_status"],
        "counts": {"skills": len(docs), "sections": len(sections), "chunks": len(chunks),
                   "indexed_chunks": sum(1 for c in chunks if c["indexed"]),
                   "duplicate_chunks": duplicates, "unique_chunks": len(chunks) - duplicates,
                   "corpus_chars": corpus_chars, "corpus_bytes": corpus_bytes,
                   "tail_merged": sum(1 for c in chunks if c["tail_merged"]),
                   "tokens": sum(tokens), "vocab": index["vocab"],
                   "postings": sum(len(entry["p"]) for entry in index["postings"].values()),
                   "stem_expansions": len(index["stem_forms"])},
        "chunk_chars": {"min": lengths[0], "p50": percentile(lengths, 0.5),
                        "p95": percentile(lengths, 0.95), "max": lengths[-1],
                        "mean": round(sum(lengths) / len(lengths), 1)},
        "chunk_tokens": {"min": tokens[0], "p50": percentile(tokens, 0.5),
                         "p95": percentile(tokens, 0.95), "max": tokens[-1],
                         "mean": round(sum(tokens) / len(tokens), 1)},
        "per_doc": per_doc, "warnings": {"unindexed_prose": sorted(warnings["unindexed_prose"]),
                                         "tail_merges": warnings["tail_merges"]},
        "vector_gate": _gate(corpus_bytes, len(docs), eval_recall),
    }


def _gate(corpus_bytes, skill_count, recall):
    reasons = []
    if corpus_bytes > GATE_THRESHOLDS["corpus_bytes"]:
        reasons.append(f"corpus_bytes={corpus_bytes}>{GATE_THRESHOLDS['corpus_bytes']}")
    if skill_count > GATE_THRESHOLDS["skill_count"]:
        reasons.append(f"skill_count={skill_count}>{GATE_THRESHOLDS['skill_count']}")
    pdf_count = count_pdfs()
    if pdf_count >= GATE_THRESHOLDS["pdf_count"]:
        reasons.append(f"pdf_count={pdf_count}")
    if isinstance(recall, (int, float)) and recall < GATE_THRESHOLDS["lexical_recall_at5"]:
        reasons.append(f"lexical_recall_at5={recall}<{GATE_THRESHOLDS['lexical_recall_at5']}")
    return {"vector_enabled": bool(reasons), "reasons": reasons,
            "decision": "vectors-justified" if reasons else "lexical-only",
            "measured": {"corpus_bytes": corpus_bytes, "skill_count": skill_count,
                         "pdf_count": pdf_count, "lexical_recall_at5": recall},
            "thresholds": dict(GATE_THRESHOLDS),
            "note": "embeddings stay off until a threshold trips; R4 writes eval-report.json"}


def count_pdfs():
    total = 0
    for base in (ROOT / "skills", ROOT / "data"):
        if base.exists():
            total += sum(1 for path in base.rglob("*") if path.suffix.lower() == ".pdf")
    return total


def _manifest(sources, docs, chunks, content_status):
    return {"format": FORMAT, "pipeline": PIPELINE, "content_status": content_status,
            "source_of_truth": "skills/*/skill.json via build_catalog.load_skills",
            "sections": [{"section": section, "field": field, "label": label}
                         for field, section, label in SECTION_FIELDS],
            "metadata_as_keywords": list(DOC_KEYWORD_FIELDS) + ["tags"],
            "gate_thresholds": dict(GATE_THRESHOLDS),
            "inputs": [{"path": sources[doc["skill_id"]]["path"],
                        "bytes": sources[doc["skill_id"]]["bytes"],
                        "sha256": sources[doc["skill_id"]]["sha256"]} for doc in docs],
            "counts": {"skills": len(docs), "chunks": len(chunks)}}


def verify(chunks, sections):
    """Assert the lossless/provenance contract on the structures we are about to write."""
    seen_ids, coverage = set(), {}
    max_chars, overlap_budget = PIPELINE["max_chars"], PIPELINE["overlap_max_chars"]
    for chunk in chunks:
        for key in ("ci", "chunk_id", "chunk_hash", "source_hash", "skill_id", "section",
                    "section_label", "ordinal", "title", "text", "span", "covered",
                    "overlap_blocks", "overlap_chars", "tail_merged", "chars", "covered_chars",
                    "tokens", "citation", "indexed", "duplicate_of", "source", "content_status"):
            if key not in chunk:
                raise RagError(f"chunk missing field: {key}")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", chunk["chunk_id"]):
            raise RagError("chunk_id is not content-addressed")
        if chunk["chars"] != chunk["span"][1] - chunk["span"][0]:
            raise RagError(f"span and length disagree: {chunk['citation']}")
        if chunk["covered_chars"] != chunk["covered"][1] - chunk["covered"][0]:
            raise RagError(f"covered range and length disagree: {chunk['citation']}")
        text = sections[(chunk["skill_id"], chunk["section"])]
        if chunk["text"] != text[chunk["span"][0]:chunk["span"][1]]:
            raise RagError(f"chunk text is not its span: {chunk['citation']}")
        if chunk["covered"][0] < chunk["span"][0] or chunk["covered"][1] > chunk["span"][1]:
            raise RagError(f"covered escapes span: {chunk['citation']}")
        ceiling = max_chars + (PIPELINE["min_chars"] if chunk["tail_merged"] else 0)
        if chunk["covered_chars"] > ceiling:
            raise RagError(f"covered range exceeds the budget: {chunk['citation']}")
        if chunk["chars"] > ceiling + overlap_budget:
            raise RagError(f"chunk exceeds max_chars: {chunk['citation']}")
        if chunk["citation"] is None:
            raise RagError("chunk without a citation")
        # Identity is by content, so two positions carrying identical text share one
        # chunk_id on purpose -- but each indexed row must still be addressable once.
        if chunk["indexed"]:
            if chunk["chunk_id"] in seen_ids:
                raise RagError("indexed chunk_id collision")
            seen_ids.add(chunk["chunk_id"])
        elif chunk["duplicate_of"] != chunk["chunk_id"]:
            raise RagError(f"duplicate does not point at its canonical id: {chunk['citation']}")
        coverage.setdefault((chunk["skill_id"], chunk["section"]), []).append(chunk["covered"])
    for (skill_id, section), ranges in coverage.items():
        text = sections[(skill_id, section)]
        merged = []
        for start, end in sorted(ranges):
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        covered = "".join(text[start:end] for start, end in merged)
        if re.sub(r"\s+", "", covered) != re.sub(r"\s+", "", text):
            raise RagError(f"coverage lost for {skill_id}#{section}")


# -------------------------------------------------------------------- collect/load

def collect_skills():
    """Skills plus the file provenance build_catalog.load_skills discards, and the
    honesty flag copied from the published catalog (sample vs published)."""
    skills = load_skills()
    sources = {}
    for skill in skills:
        path = ROOT / "skills" / skill["id"] / "skill.json"
        raw = path.read_bytes()
        sources[skill["id"]] = {"path": str(path.relative_to(ROOT)), "bytes": len(raw),
                                "sha256": sha256_hex(raw)}
    status = "sample"
    published = ROOT / "docs/data/index.json"
    if published.exists():
        data = json.loads(published.read_text(encoding="utf-8"))
        if data.get("skills") != skills:
            raise RagError("docs/data/index.json is stale; run scripts/build_catalog.py first")
        if data.get("sample") is not True:
            status = "published"
    return skills, sources, status


# ------------------------------------------------------------------------- outputs

def dumps(value, indent=2):
    """Same shape as build_catalog.json_bytes, but as text (render encodes once)."""
    if indent:
        return json.dumps(value, ensure_ascii=False, indent=indent) + "\n"
    # The search index is machine input for R2/R3, so it stays compact: pretty
    # printing it multiplied its size ~2.5x for no benefit.
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"


def render(corpus, index, manifest, stats):
    files = {
        "corpus.jsonl": "".join(json.dumps(chunk, ensure_ascii=False) + "\n"
                                 for chunk in corpus["chunks"]),
        "index.json": dumps(index, indent=0),
        "stats.json": dumps(stats),
    }
    files["manifest.json"] = dumps({**manifest, "artifacts": {
        name: {"bytes": len(content.encode("utf-8")), "sha256": sha256_hex(content.encode("utf-8"))}
        for name, content in sorted(files.items())}})
    return files


def read_eval_recall(out_dir):
    path = Path(out_dir) / "eval-report.json"
    if not path.exists():
        return None
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    metrics = report.get("metrics") if isinstance(report, dict) else None
    if isinstance(metrics, dict):
        for key in ("recall_at5", "recall@5", "recall"):
            if isinstance(metrics.get(key), (int, float)):
                return float(metrics[key])
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="fail unless the committed artifacts are exactly what this code builds")
    parser.add_argument("--out", default=str(ROOT / "data/rag"))
    parser.add_argument("--print-stats", action="store_true")
    parser.add_argument("--verify", action="store_true",
                        help="report the contract check result (it always runs; this prints it)")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        return self_test()
    out_dir = Path(args.out)
    try:
        skills, sources, status = collect_skills()
        corpus, index, manifest, stats, _sections = build(
            skills, sources, status, read_eval_recall(out_dir))
    except (RagError, ValueError) as error:
        print(f"rag_index: {error}", file=sys.stderr)
        return 1
    files = render(corpus, index, manifest, stats)
    if args.check:
        problems = []
        for name, content in files.items():
            path = out_dir / name
            payload = content.encode("utf-8")
            if not path.exists():
                problems.append(f"missing {name}")
            elif path.read_bytes() != payload:
                problems.append(f"outdated {name}; run scripts/rag_index.py")
        if problems:
            print("rag_index: " + "; ".join(problems), file=sys.stderr)
            return 1
    else:
        out_dir.mkdir(parents=True, exist_ok=True)
        for name, content in files.items():
            (out_dir / name).write_bytes(content.encode("utf-8"))
    counts, gate = stats["counts"], stats["vector_gate"]
    if args.print_stats:
        print(f"skills={counts['skills']} sections={counts['sections']} "
              f"chunks={counts['chunks']} indexed={counts['indexed_chunks']} "
              f"dupes={counts['duplicate_chunks']} chars={counts['corpus_chars']} "
              f"vocab={counts['vocab']} postings={counts['postings']}")
        print(f"covered chars p50={stats['chunk_chars']['p50']} "
              f"p95={stats['chunk_chars']['p95']} max={stats['chunk_chars']['max']}")
        print(f"vector gate: {gate['decision']}"
              + (f" ({', '.join(gate['reasons'])})" if gate["reasons"] else "")
              + f" | unicodedata={unicodedata.unidata_version}")
    if args.verify:
        print(f"rag_index: contract verified for {counts['chunks']} chunks / "
              f"{counts['sections']} sections (lossless, provenance-complete).")
    target = out_dir.relative_to(ROOT) if str(out_dir).startswith(str(ROOT)) else out_dir
    print(f"{'Checked' if args.check else 'Wrote'} {len(files)} files -> {target}/")
    return 0


# ------------------------------------------------------------------------ self-test

def self_test():
    """Offline proof of the contract; CI never has to trust the committed bytes."""
    failures = []
    long_prompt = "اسأل عن الجمهور والهدف. " * 120
    skill = {"id": "SKL001", "name": "كتابة المحتوى", "category": "كتابة",
             "difficulty": "متوسط", "icon": "pen", "color": "peach",
             "description": "حوّل فكرة إلى نص واضح. ساعدني في تحديد الجمهور.",
             "starter": "أريد كتابة منشور قصير عن إطلاق منتج.",
             "prompt": long_prompt, "tags": ["محتوى", "تسويق"]}
    twin = dict(skill, id="SKL002", name="مرآة", description="حوّل فكرة إلى نص واضح. ساعدني في تحديد الجمهور.")
    sources = {key: {"path": f"skills/{key}/skill.json", "bytes": 12,
                     "sha256": sha256_hex(key.encode())} for key in (skill["id"], twin["id"])}
    try:
        first = render(*build([skill, twin], sources, "sample")[:4])
        second = render(*build([skill, twin], sources, "sample")[:4])
        if first != second:
            failures.append("build is not deterministic")
        for name, content in first.items():
            if not content.endswith("\n"):
                failures.append(f"{name} does not end with a newline")
        if not first["corpus.jsonl"].startswith('{"ci": 0'):
            failures.append("corpus.jsonl field order is not stable")
        corpus, index, _manifest, stats = build([skill, twin], sources, "sample")[:4]
        chunks = corpus["chunks"]
        if len(stats["counts"]) < 8:
            failures.append("stats counts look thin")
        # prompt text must have split into several chunks, all within budget
        prompt_chunks = [c for c in chunks if c["section"] == "prompt"]
        if len(prompt_chunks) < 2:
            failures.append("long prompt was not split")
        for chunk in prompt_chunks:
            ceiling = PIPELINE["max_chars"] + (PIPELINE["min_chars"] if chunk["tail_merged"] else 0)
            if chunk["covered_chars"] > ceiling:
                failures.append(f"chunk over budget: {chunk['citation']}")
        # overlap must be a real prefix of the chunk and must exist only in span
        for chunk in prompt_chunks:
            if chunk["overlap_chars"] != chunk["chars"] - chunk["covered_chars"]:
                failures.append(f"overlap accounting disagrees at {chunk['citation']}")
            if chunk["covered"][0] != chunk["span"][0] + chunk["overlap_chars"]:
                failures.append(f"covered start is not span start + overlap at {chunk['citation']}")
        # dedup: SKL002#description has the same text as SKL001#description
        dupes = [c for c in chunks if c["duplicate_of"]]
        if not any(d["skill_id"] == "SKL002" and d["section"] == "description" for d in dupes):
            failures.append(f"cross-skill duplicate was not deduped: "
                            f"{[(d['skill_id'], d['section']) for d in dupes]}")
        if not dupes:
            failures.append("no duplicate found in a corpus built from cloned text")
        if any(c["duplicate_of"] and c["indexed"] for c in chunks):
            failures.append("duplicate was indexed anyway")
        indexed = {c["ci"] for c in chunks if c["indexed"]}
        for term, entry in index["postings"].items():
            for ci, _tf in entry["p"]:
                if ci not in indexed:
                    failures.append(f"posting {term!r} points at an unindexed chunk")
                    break
        if index["postings"] != dict(sorted(index["postings"].items())):
            failures.append("postings are not sorted by term")
        if any(v != sorted(v) for v in index["stem_forms"].values()):
            failures.append("stem expansion surfaces are not sorted")
        citations = [c["citation"] for c in chunks]
        if len(citations) != len(set(citations)):
            failures.append("citations are not unique")
        if not all(re.fullmatch(r"SKL00\d#(description|prompt|starter)(:\d+)?", c) for c in citations):
            failures.append(f"citation format: {citations[:2]}")
        # identity is content per section: editing starter must not re-identify prompt
        edited = build([dict(skill, starter="مثال آخر تمامًا."), twin], sources, "sample")[0]
        if {c["chunk_id"] for c in prompt_chunks} - {c["chunk_id"] for c in edited["chunks"]}:
            failures.append("editing one section re-identified another section's chunks")
        # re-chunking the same section text must keep every chunk_id
        same = build([dict(skill, prompt=long_prompt + "  "), twin], sources, "sample")[0]
        if {c["chunk_id"] for c in prompt_chunks} != {c["chunk_id"] for c in same["chunks"]
                                                        if c["section"] == "prompt"}:
            failures.append("whitespace-only change altered chunk identities")
        # ...but splitting a section differently does change chunk text (and thus ids):
        # that is intended, so citations must be checked by R2, never guessed.
        if not any(c["duplicate_of"] is None for c in chunks):
            failures.append("no canonical chunk in the corpus")
        # gate: tiny corpus => lexical only; huge corpus or bad recall => vectors
        if stats["vector_gate"]["decision"] != "lexical-only" or stats["vector_gate"]["vector_enabled"]:
            failures.append("gate enabled vectors for a tiny corpus")
        huge = build([dict(skill, description="كلمة " * 60000)],
                     {skill["id"]: sources[skill["id"]]}, "sample")[3]
        if not huge["vector_gate"]["vector_enabled"]:
            failures.append("gate ignored a corpus past the byte threshold")
        recalled = build([skill, twin], sources, "sample", eval_recall=0.41)[3]
        if not recalled["vector_gate"]["vector_enabled"]:
            failures.append("gate ignored measured recall")
        # normalization must keep diacritics in text, and drop them only for search
        diacritized = dict(skill, description="بِسْمِ اللهِ الرَّحْمٰنِ الرَّحِيمِ")
        sample = build([diacritized], {diacritized["id"]: sources[skill["id"]]}, "sample")[0]
        stored = sample["chunks"][0]["text"]
        if "ِ" not in stored:
            failures.append("normalize_text stripped diacritics from evidence text")
        if content_terms("الرَّحْمٰنِ") != content_terms("الرحمن"):
            failures.append("search normalization does not fold diacritics")
        if tokenize("محتوى، تسويق!") != ["محتوي", "تسويق"]:
            failures.append(f"punctuation/alef folding wrong: {tokenize('محتوى، تسويق!')}")
        if tokenize("Hello, World! 2026") != ["hello", "world", "2026"]:
            failures.append("latin tokenizing is not word-boundary based")
        if content_terms("في البيت the cat") != ["البيت", "cat"]:
            failures.append("stopwords are not filtered from content terms")
        # unindexed prose must be reported, not swallowed
        leaky = dict(skill, extra_prose="هذه حقل نثري جديد طويل بما يكفي ولن يُفهرس بصمت، "
                                        "لذلك يجب أن يظهر في التحذيرات حتى لا يضيع المحتوى")
        warned = build([leaky], {skill["id"]: sources[skill["id"]]}, "sample")[3]
        if "SKL001.extra_prose" not in warned["warnings"]["unindexed_prose"]:
            failures.append("unindexed prose was silently dropped")
        # an empty corpus is vacuously valid (a catalog with no skills cannot exist:
        # load_skills() refuses it), so verify() must not invent an error here
        try:
            verify([], {})
        except RagError as error:
            failures.append(f"verify rejected an empty corpus: {error}")
    except Exception as error:  # noqa: BLE001 - a self-test reports, it never traces
        failures.append(f"{type(error).__name__}: {error}")
    if failures:
        for failure in dict.fromkeys(failures):
            print(f"rag_index self-test FAIL: {failure}", file=sys.stderr)
        return 1
    print("rag_index self-test: ok (determinism, lossless coverage, dedup, citations, "
          "identity stability, gate, normalization)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
