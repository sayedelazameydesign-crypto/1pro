# فهرس الاسترجاع (مرحلة R1)

**ملفات مولَّدة — لا تُحرَّر يدويًا.** المصدر هو `skills/<ID>/skill.json`، ويُبنى بالأمر:

```bash
python scripts/rag_index.py          # كتابة
python scripts/rag_index.py --check  # تحقق CI (لا يكتب شيئًا)
python scripts/rag_index.py --print-stats
```

| الملف | الدور |
|---|---|
| `corpus.jsonl` | مقطع واحد في كل سطر؛ النص كامل مع `span`/`covered` و`citation`. هذا هو المرجع الذي تثق به R2/R3/R4. |
| `index.json` | فهرس معجمي جاهز لـ BM25: `postings` (مصطلح → `df` و`[ci, tf]`)، `chunk_len`، و`stem_forms` كتوسيع عكسي لا كبديل للاسطح. مضغوط لأنه مدخل آلة لا ملف يُقرأ بالعين. |
| `manifest.json` | أي الملفات دخلت البناء، بايتاتها و`sha256` لكل مخرَج، و`pipeline` بإصداراته. |
| `stats.json` | العدادات والتوزيعات، و**`vector_gate`**: قرار التضمين المحسوب. |

## المستهلكون

| مرحلة | كيف تقرأ هذه الملفات |
|---|---|
| **R2** (`backend/rag_search.py` → `GET /api/search`) | **قراءة فقط**: `index.json` للدرجات و`corpus.jsonl` للنص؛ لا يعيد البناء ولا يغيّر schema. `chunk_id`/`citation` في الردّ هما سلسلتا R1 حرفيًا، و`index.manifest_sha256` في كل استجابة هو sha256 لـ`index.json` نفسه |
| **R3** (`kb_search` في `agent/tools.py`) | تستدعي `rag_search.search()` نفسها — لا مسترجِع ثانيًا داخل الوكيل |
| **R4** (`scripts/rag_eval.py`) | تبني الأسئلة من `corpus.jsonl` وتكتب `data/rag/eval-report.json`، الملف الوحيد المسموح له بتغيير بوابة R5 |

لا شيء هنا يُضمَّن ولا يُبني وقت الطلب: الدليل كامل في المستودع، و`scripts/rag_index.py --check`
يفشل CI إن باعته المخرجات شيئًا مختلفًا.

## العقد الذي يضمنه `scripts/rag_index.py`

- **حتمي:** نفس المصدر + نفس خط الأنابيب = نفس البايتات. لا طابع زمني في أي مخرَج، لذلك يبقى `--check` صالحًا للأبد.
- **بلا فقد:** `text == normalized[span]`، والاتحاد بين `covered` يغطي كل حرف غير مسافات في القسم؛ التراكب يُكرَّر ولا يُسقَط أبدًا.
- **مُوثَّق المصدر:** `source` + `source_hash` (ملف المصدر) + `chunk_hash` (نص المقطع) + `citation` قابلة للذوبان مثل `SKL001#prompt:2`. الجواب إما يستشهد بها أو لا يدّعي مصدرًا.
- **الهوية بالمحتوى:** `chunk_id = sha256(format, skill_id, section, chunk_hash)`؛ تعديل قسم لا يُعيد ترقيم أقسام أخرى.
- **التسامح مُعلَن:** مقطع ذيل أقصر من `min_chars` يُدمج مع سابقه ويحمل `tail_merged: true` بدل أن يبقى شظية لا تُسترجع.

## بوابة المتجهات (R5)

لا أحد يقرّر «صارت كبيرة» شعوريًا: `stats.json.vector_gate` يحسبها من
`corpus_bytes > 200000` أو `skill_count > 50` أو وجود PDF أو
`recall@5 < 0.80` المقاس في `data/rag/eval-report.json` (من مرحلة R4).
طالما القرار `lexical-only`، لا استدعاء Embedding ولا مفتاح NVIDIA في أي مكان.
