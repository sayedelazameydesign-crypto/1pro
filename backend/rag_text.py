"""Shared text primitives for the RAG pipeline: one tokenizer for both stages.

R1 (`scripts/rag_index.py`) builds the index with these rules and R2
(`backend/rag_search.py`) folds queries with the same functions. They live in
`backend/` rather than `scripts/` for a deployment reason, not an aesthetic one: Vercel
ships `backend/` but excludes `scripts/**`, and a query-time copy of the tokenizer
would silently disagree with the postings it scores against -- the kind of defect no
test notices until retrieval quietly gets worse.

Two layers on purpose:

* `normalize_text` -- display hygiene only. Stored evidence keeps every character the
  author wrote, including Arabic diacritics, because chunk text is a quotation.
* `normalize_search` -- lossy folding (diacritics, alef/ya variants, punctuation) used
  only for keys. Folding the stored text would rewrite the source to flatter the ranker.
"""
import re
import unicodedata

# Versioned so an index and a query can prove they speak the same language.
TOKENIZER = "waha.word-ar-lite/1"
STEMMER = "waha.affix-lite/1"

STOPWORDS = frozenset("""
في على من إلى عن مع هذا هذه ذلك التي الذي هو هي ما لا أن إن لكن أو ثم قد هل كل
بعض بين عند لدى كما لأنه لأنها بحيث هنا هناك يا أي ماذا كيف لماذا متى ال و ف ب ك ل
بل نحو حتى إذا لم لن غير ذات عين هو هي هما هم هن ان انمااما
the a an and or to in for of on with that this it is are was were be been as at by
from not no yes you your we our they their i he she its will would can could should
do does did have has had which who whom what when where why how all any some more
""".split())
ARABIC_MARKS = re.compile(r"[ً-ْٰـ]")
ALEF_FAMILY = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ى": "ي",
                             "ؤ": "و", "ئ": "ي", "ة": "ه", "ﻻ": "لا"})
PUNCT_FOLD = str.maketrans({"،": ",", "؛": ";", "؟": "?", "–": "-", "—": "-",
                            "‘": "'", "’": "'", "“": '"', "”": '"'})
WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
PARAGRAPH_SPLIT = re.compile(r"\n\s*\n")
SENTENCE_SPLIT = re.compile(r"(?<=[.!?؟])\s+")
PREFIXES = ("وال", "بال", "فال", "كال", "لل", "ال", "و", "ف", "ب", "ك", "ل")
SUFFIXES = ("اتهم", "اتها", "ون", "ين", "ات", "ها", "هم", "كن", "نا", "ية", "يه",
            "اً", "ًا", "ة", "ى", "ه")


def normalize_text(value):
    """Display hygiene only: keeps every character that carries meaning.

    Spelling folds that lose information (diacritics, alef variants) happen in
    normalize_search, so the evidence stored in ``text`` stays faithful.
    """
    text = unicodedata.normalize("NFKC", str(value)).replace("\r\n", "\n").replace("\r", "\n")
    out, blank = [], 0
    for line in (raw.strip() for raw in text.split("\n")):
        if line:
            out.append(line)
            blank = 0
        elif out and blank == 0:
            out.append("")
            blank = 1
    return "\n".join(out).strip()


def normalize_search(text):
    return ARABIC_MARKS.sub("", text).translate(ALEF_FAMILY).translate(PUNCT_FOLD).lower()


def tokenize(text):
    return WORD_RE.findall(normalize_search(text))


def content_terms(text):
    return [term for term in tokenize(text) if term not in STOPWORDS and len(term) > 1]


def stem(word):
    """Cheap affix stripping used ONLY to build an expansion dictionary; surface
    terms stay canonical, so a wrong guess cannot lose recall for anyone."""
    best = word
    arabic = "\u0621" <= word[:1] <= "\u064a"
    for prefix in PREFIXES:
        residual = best[len(prefix):]
        if best.startswith(prefix) and len(residual) >= 3 and (
                not arabic or "\u0621" <= residual[:1] <= "\u064a"):
            best = residual
            break
    for suffix in SUFFIXES:
        if best.endswith(suffix) and len(best) - len(suffix) >= 3:
            best = best[: -len(suffix)]
            break
    return best if len(best) >= 3 else word
