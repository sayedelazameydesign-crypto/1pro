# واحة · Waha MVP + GitHub

قالب جاهز للرفع إلى المستودع المقترح:
https://github.com/sayedelazameydesign-crypto/1pro

**الحالة:** `main` يحمل الآن الكتالوج **وطبقات R1→R6** (دُمج PR #8 عند `9a7af62`؛ CI أخضر
على `sqlite` و`postgres` معًا: 553 اختبار بايثون + 15 فحص عقد متصفح + 15 فحص عقد مكوّنات + 18 فحص عقد تكاملات + فحوص البايت
`rag_index --check` و`rag_eval --check`). الكتالوج منشور على Pages ويعمل وأُعلن الإصدار
`v0.1.0`. **الخادم منشور الآن** على Vercel عند `https://cela-umber.vercel.app` من `main`
(`72371d6`): `/health` يردّ 200، و`/api/me` يردّ هوية صالحة، والمسار غير الموجود يُرجع 404
من Flask نفسه — أي أن إصلاح الـ catch-all (PR #12) صامد. و`docs/data/config.json.api_base`
يشير إليه، فـ Pages يخرج من وضع الكتالوج الثابت بعد أول إعادة نشر.

**ما لم يحدث بعد:** الخادم المنشور بلا مفاتيح إنتاج — `/health` الحيّ يقول
`"database":"sqlite"` و`"ai":"disabled"`، أي أن `DATABASE_URL` (Neon) و`GEMINI_API_KEY`
و`WAHA_SECRET` و`WAHA_ALLOWED_ORIGINS` غير مضبوطة في بيئة Vercel. على Vercel يسكن
SQLite في `/tmp` الزائل، فكل محادثة تضيع مع إعادة التدوير: **لا تعتبره نشرًا حقيقيًا قبل
ضبط `DATABASE_URL`.** الطريق: `scripts/vercel_env_sync.sh` (أو لصق المتغيرات يدويًا) ثم
إعادة نشر Pages وتفعيل Discussions. كود المشروع مرخّص بموجب Apache-2.0
(راجع `LICENSE`). حالة CI الفعلية تُعرض في Actions.

## نسختان واضحتان

| النسخة | ما يعمل | ما لا يعمل |
|---|---|---|
| `docs/` · GitHub Pages | واجهة عربية RTL، ألوان هادئة، dark mode، بحث وفلاتر، تنزيل 6 مهارات JSON؛ ومحادثة عامة ومكتبة المحادثات ومساحة العمل تظهر عند ضبط `api_base` والاتصال بالخادم | لا Python، لا SQLite، لا حفظ محادثات أو AI من Pages وحدها |
| `backend/` · تطبيق الخادم | مكتبة خاصة، شرح/تمارين/اختبارات، محادثة عامة مع المساعد الذكي، وحفظ المحادثات وتصديرها وحذفها | يعمل في وضعين: خلف بوابة PromptQL، أو وضع مستقل (انظر «النشر المستقل») |
| `backend/agent/` · طبقة الوكيل | خطة متعددة الخطوات، أدوات بموافقة، حالة مهمة محفوظة، بث أحداث SSE، لوحة مخرجات (artifacts)، ذاكرة متعلّم، وتنفيذ كود داخل حدود عزل (`AGENT_CODE_EXEC=1`) | لا متصفح، لا طرفية، لا ملفات مستخدم، وعزل نظام الملفات غير موجود — و`code_exec` تبليغها صريح، ولا مهام تتجاوز عمر الطلب/الخطة المجانية |
| `backend/marketplace/` · متجر Vercel | واحة **مزوّدًا** في متجر Vercel: تثبيت OAuth2، تحقّق من توكنات Vercel، توفير مساحة عمل لكل مورد، حقن `WAHA_API_BASE`/`WAHA_API_TOKEN`/`WAHA_WORKSPACE_ID` في مشاريع العملاء، تدوير يبطل القديم فعلًا، ولوحة عربية بلا أسرار | لا فوترة عبر Vercel، لا استيراد موارد قائمة، لا فحوص نشر — و`C96` و`C97` في المصفوفة تقولان ذلك صراحةً |

**Pages تستضيف ملفات ثابتة فقط.** ربطها بخادم خارجي يتطلب نظام دخول
وتحقق من الهوية وسياسة CORS وحماية الأسرار وإعداد موفّر AI منفصل.
لا تضع مفتاح Gemini أو توكن PromptQL في JavaScript أو المستودع.

الوضع المستقل يوفر هذه المتطلبات: هوية زائر يصدرها الخادم نفسه (توكن
موقّع بـ HMAC)، حماية CSRF، قائمة CORS محصورة، وحدود طلبات لكل مستخدم
ولكل عنوان IP. الطبقة المجانية لـ Gemini قد تستخدم محتوى الطلبات لتحسين
منتجات Google؛ لا ترسل بيانات حساسة.

التطبيق الكامل الحالي يظل يعمل في VM على PromptQL؛ هذا القالب لا يغيره.

## البنية

```text
backend/             تطبيق Flask الحالي وملف إعداد غير سري
backend/agent/       طبقة الوكيل: providers · tools · store · runtime · service
backend/marketplace/  تكامل متجر Vercel بصفة المزوّد: OAuth2 · ويب هوك · توفير مورد · حقن متغيرات بيئة
docs/                الواجهة الثابتة التي يمكن نشرها على Pages
skills/<ID>/         المصدر الأساسي للمهارات الست
scripts/             تحقق وبناء فهرس ثابت + smoke للخادم المستقل
tests/               اختبارات بدون اتصال AI أو أسرار
.github/workflows/   CI + نشر Pages يدوي بإذن المالك
.github/ISSUE_TEMPLATE/
```

`CAPABILITY-MATRIX.md` يقابل القدرات بـClaude وManus: ٩٧ قدرة موسومة
`Implemented/Partial/Missing/Mocked` بدليل من الكود لكل صف، ومرتّبة P0/P1/P2.
`tests/test_capability_matrix.py` يفشل إذا لم يعد الدليل يتحقق — فلا تتقادم المصفوفة صامتة.

`MARKETPLACE-VERCEL.md` هو دليل تشغيل التكامل الأصلي بصفة مزوّد: نصّ التسجيل الجاهز
للّصق في نموذج Vercel، المتغيرات، المسارات، وما لا يعمل بعد.

## جرّب نسخة Pages محلياً

Python 3.10 أو أحدث مطلوب لبناء الفهرس. CI يستخدم Python 3.12.

```bash
python scripts/build_catalog.py
python -m http.server 8080 --directory docs
```

افتح http://localhost:8080
لا تفتح HTML كـ`file://`؛ المتصفح يحتاج خادماً لتحميل JSON.
مصادر المهارات نسبية، لذلك تعمل تحت `/1pro/` ولا تعتمد على روابط Raw وهمية.

## الاختبارات

```bash
python scripts/rag_index.py --check      # artifacts R1 مطابقة للبايت لما في المستودع
python scripts/rag_eval.py --check       # قياس R4 مطابقة، والبوابة تقرأ نفس الرقم
```


```bash
python -m venv .venv
# Linux / macOS:
. .venv/bin/activate
# Windows: .venv\Scripts\activate
python -m pip install -r backend/requirements.txt
python scripts/build_catalog.py --check
python -m unittest discover -s tests -v
node --check docs/assets/app.js
python scripts/deploy_doctor.py --self-test
python scripts/deploy_doctor.py --target render   # يفحص بيئة النشر عندك، بلا شبكة ولا طباعة مفاتيح
```

اختبارات طبقة الوكيل تشغّل الحلقة كاملة على `FakeProvider` (بلا شبكة): خطة، أداة،
موافقة/رفض، إلغاء، artifacts، ذاكرة، حدود الساعة المشتركة مع المحادثة، وتنعزل
الملفات بين الزوّار. اختبارات CI تستخدم هوية وAI مُحاكيين داخل عميل اختبار، وتعمل كاملةً على
قاعدتين مؤقتتين: SQLite وPostgreSQL 16. يمر اختبار NVIDIA cooldown في
وظيفة PostgreSQL لتغطية صيغة upsert الحقيقية. الرد الفعلي لـGemini اختُبر
في تطبيق PromptQL، لكنه ليس جزءاً من CI. الفحص البنيوي لا يثبت صحة المحتوى
أو جاهزية إنتاجية شاملة.

## رفع القالب

بعد مراجعة الملفات، ارفعها إلى الفرع الذي تختاره.
المستودع الهدف كان عاماً وبلا ملفات ظاهرة عند الفحص؛ أي شيء ترفعه إليه قد
يصبح متاحاً للآخرين.

```bash
git init
git branch -M main
git add .
git commit -m "Add Waha MVP template and static skill catalog"
git remote add origin https://github.com/sayedelazameydesign-crypto/1pro.git
# فقط بعد مراجعتك وموافقتك:
git push -u origin main
```

لا تستخدم force push. إذا ظهر أن المستودع يحتوي تغييرات، اجلبها أولاً واعمل
على فرع منفصل بدلاً من استبدالها. لا تشارك مفاتيح GitHub في المحادثة.

## تفعيل Pages — بعد الرفع

1. في إعدادات المستودع: **Pages → Source → GitHub Actions**.
2. افتح **Actions → Publish static skill catalog to Pages → Run workflow**.
3. وافق على النشر إذا طلبت بيئة `github-pages` ذلك.
4. استخدم الرابط الفعلي الذي يعرضه GitHub بعد نجاح المهمة؛ لم يُنشأ رابط حي بعد.

النشر متعمد أن يكون **يدوياً** في هذه النسخة حتى لا يصبح المحتوى عاماً
بمجرد push. CI يعمل عند push وPR للتحقق دون مفاتيح أو صلاحيات كتابة.
الـActions الرسمية مثبتة عند commit محدد؛ حدّثها بعد المراجعة.

لا ملف CNAME قبل اختيار نطاق وتهيئة DNS.

## طبقة الاسترجاع · R2 (بحث معجمي فوق فهرس Waha)

`backend/rag_search.py` يقرأ `data/rag/` كما هو — لا مخطط موازٍ، لا إعادة توليد، لا
استدعاء شبكة ولا قاعدة بيانات في مسار البحث. BM25 فوق `postings` المودَعة، بوزن للقسم
داخل التراكم (`prompt` 1.15، `description` 1.0، `starter` 0.55 — القيم الثلاث في
`rag_search.SECTION_WEIGHTS`، وأي رقم في هذا الملف يخالفها يفشله `tests/test_docs_match_code.py`)
وعبارات إيقافية تُطبَّق
على الاستعلام وحده. قواعد الطيّ نفسها في `backend/rag_text.py` يستوردها R1 وR2 معًا، لأن
نسخة ثانية من المجزّئ كانت ستختلف عن `postings` بصمت.

| المسار | الوظيفة |
|---|---|
| `GET /api/search?q=&k=&section=&skill=` | نتائج مرتبة فوق فهرس R1: `citation` و`chunk_id` حرفيان من المقطع، مع `score` و`explain` و`coverage`. `k` حتى 20 (الأكثر يُقصّ لا يُرفض). **لا عتبة قرار**: `no_answer.decision` إما `deferred` أو `unsearchable`، ومن يملك العتبة هو `data/rag/eval-report.json` في R4 |

**R3**: `kb_search` داخل حلقة الوكيل هي نفس الدالة — للقراءة فقط، بلا موافقة، بسقف 5
نتائج و`MAX_KB_CONTEXT_CHARS=3000` حرف من الأدلة لكل لفّة، ومع `citation` مُمرَّرًا حرفيًا.
فهرس مفقود هناك يُبلَّغ `ToolError` («المكتبة غير متاحة») لا «لا نتائج»، حتى لا يُلغَس عطل
بنية تحتية في مقياس `no-answer-rate`.

بلا فهرس → `503 rag_index_missing` مع السبب في `/health.rag.reason`. والواجهة
(`docs/assets/search.js`) تسمّي مصدرها دائمًا: `RAG_LOCAL` من الخادم، أو
`BROWSER_FALLBACK` عند انقطاعه أو انتهاء المهلة (2.5 ثانية) فتعرض التصفية المحلية
موسومةً صراحة بأنها **ليست استرجاع RAG** وبلا قائمة «نتائج»، أو `NONE` بلا `api_base`.
لا يُمزج المساران في قائمة واحدة، ولا يُرسَل الكتالوج إلى المتصفح.

**R4 (التقييم):** `python scripts/rag_eval.py` يقيس المسترجِع نفسه عبر 22 حالة في
`eval/train.json` ويكتب `data/rag/eval-report.json` — وهو **الكاتب الوحيد** له. الوضع
المُراجَع `--check` يعيد الحساب ويقارن البايتات ويفشل CI عند الانحراف أو عند `false-answer > 0`،
وهو المصدر الوحيد لبوابة المتجهات في `stats.json`. القياس الحالي:
`recall@5 = 0.9375` · `precision@1 = 0.9375` · `no-answer = 1.0` · `citations = 1.0`
· `dupes = 0.0` ← **المتجهات ما زالت غير مبرَّرة**. الفجوتان المعروفتان (التعريب
`بايثون`≠`python`، والاستعلام بلفظ اسم المهارة) تبقيان في الاختبار عمداً.

## طبقة الوكيل · Agent Runtime

`backend/agent/` تحوّل المحادثة أحادية الرد إلى حلقة: **افهم → خطّط → نفّذ → راقب →
راجع → أبلغ**، بحالة محفوظة في القاعدة لا في ذاكرة العملية. القرارات والمرفوضات في
`ARCHITECTURE.md`.

| المسار | الوظيفة |
|---|---|
| `GET /api/agent/config` | enabled/model/tools/limits (بلا أسرار، `guaranteed_capacity:false`) |
| `POST /api/agent/tasks` | `{goal, provider?}` → مهمة؛ `agent_busy` إن كانت لديك مهمة نشطة |
| `GET /api/agent/tasks[/<id>]` | قائمة المهام أو مهمة واحدة: خطة، خطوات، أدوات، artifacts، usage |
| `GET /api/agent/tasks/<id>/events?cursor=` | سجل الأحداث (المصدر الصادق للحالة) |
| `GET /api/agent/tasks/<id>/stream` | نفس الأحداث بصيغة SSE؛ يستلزم `--threads` في gunicorn |
| `POST /api/agent/tasks/<id>/approve` | `{call_id, approve}` — بلا موافقة لا يُنفَّذ شيء |
| `POST /api/agent/tasks/<id>/cancel\|delete` | إلغاء تعاوني أو حذف مع الخطوات والأحداث |
| `GET /api/agent/artifacts/<id>` | ملف من لوحة العرض (html/css/js/json/markdown) |
| `GET/POST /api/agent/memory` · `…/<id>/delete` | ملاحظات المتعلّم التي تُقرأ كسياق فقط |

**الأدوات المبنية:** `calculator` (تعبير رقمي بحارس AST)، `clock`، `skill_lookup`، `kb_search`،
`memory_write`، `artifact_write`، `web_fetch` (GET مع حارس SSRF، معطّل افتراضياً
ويحتاج موافقة)، و`code_exec` (R7: برنامج بايثون قصير داخل حدود عزل — معطّل افتراضياً
ويحتاج موافقة). **المرفوض عمداً:** shell، كتابة ملفات، متصفح، ومهام تتجاوز عمر الطلب.

**حدود `code_exec` — ما تفرضه وما لا تفرضه.** الأداة لا تعمل إلا إذا استطاع الخادم بناء
الحدود كاملة، وإلا رفضت بلا تنفيذ. و«حدود ناقصة» ليست تفصيلًا نظريًا: نسخة سابقة كانت
تعزل الشبكة وحدها، فمرّ الاختبار الذي يحاول الكتابة في ملف على المضيف ونجح — لذلك صار
الجذر الخاص شرطًا في `REQUIRED_CAPABILITIES`، لا تحسينًا اختياريًا. الفرض مُثبت باختبارات
تُطلق الفشل عمدًا (`tests/test_agent_sandbox.py`، ٤١ فحصًا): البرنامج يعمل `pid 1` داخل
مساحات أسماء مستخدم وmount وPID وشبكة، بجذر ملفات لا يحتوي شيئًا من الخادم — وقت التشغيل
للقراءة فقط و`/workspace` و`/tmp` وحدهما قابلان للكتابة — والشبكة مقطوعة على مستوى النواة
(الاختبار يحاول اتصالًا حقيقيًا ويلزمه بالفشل)، والبيئة قائمة بيضاء من أربعة ثوابت لا ترث
متغيرًا واحدًا، والذاكرة ووقت المعالج وعدد العمليات وحجم الملفات وسقف الإخراج كلها
`setrlimit` يفرضها الطفل على نفسه قبل تجميع أي سطر، والمهلة تقتل **مجموعة العمليات** فلا
ينجو حفيد. وأقوى دليل هو الأبسط: برنامج يحاول فتح مسار على المضيف للكتابة فينتهي بـ
`FileNotFoundError`، ثم يُفحص المضيف نفسه بعد الجولة ليتأكد أن الملف غير موجود — لا أن
الإخراج يقول ذلك.

**وما لا تفرضه، بصريح العبارة:** لا cgroup على هذا الخادم (`mkdir` تحت
`/sys/fs/cgroup` مرفوض)، فسقف الذاكرة **لكل عملية** لا للشجرة كاملة. لذلك تُبلَّغ كل نتيجة
بأسوأ حالة ممكنة (`aggregate_memory_bound_mb` = الذاكرة × سقف العمليات = 128 × 4 = 512MB)
بدل رقم واحد يُقرأ كأنه كلي. `RLIMIT_NPROC`/`RLIMIT_AS` دفاع داخلي لا بديل عن سقف كلي.
لذلك `C25` في `CAPABILITY-MATRIX.md` تبقى `Partial` بعد أن صار عزل الملفات والعملية قائمًا
ومُختبرًا: المتبقي هو السقف الكلي، لا ادّعاء العزل.

| متغير | الافتراضي | ملاحظات |
|---|---|---|
| `AGENT_CODE_EXEC` | 0 | 1 يفتح `code_exec` (ويبقى محتاجًا موافقة، ويبقى مشروطًا بوجود حدود) |
| `AGENT_CODE_EXEC_TIMEOUT_SECONDS` | 15 | 1–120؛ المهلة تقتل مجموعة العمليات كاملة |
| `AGENT_CODE_EXEC_MEMORY_MB` | 128 | 32–2048؛ `RLIMIT_AS` **لكل عملية**؛ الرقم الكلي = هذا × التالية |
| `AGENT_CODE_EXEC_MAX_PROCESSES` | 4 | 1–32؛ `RLIMIT_NPROC` يحصي **المتزامنة** لا المتتابعة (اختبار يفتح ٢٤ عملية ويعدّ المحجوب) |
| `AGENT_CODE_EXEC_CPU_SECONDS` | 10 | 1–120؛ `RLIMIT_CPU` |
| `AGENT_CODE_EXEC_MAX_OUTPUT_BYTES` | 4000 | 500–40000؛ الزائد يُقصّ ويُعلَن `truncated` |

| متغير | الافتراضي | ملاحظات |
|---|---|---|
| `AGENT_MAX_STEPS` | 5 | 1–10؛ كل خطوة استدعاء نموذج على الأقل |
| `AGENT_MAX_TOOL_CALLS` | 8 | سقف أدوات المهمة الواحدة |
| `AGENT_MAX_AI_CALLS` | 8 | لا يتجاوز حدود الساعة المشتركة مع المحادثة |
| `AGENT_TASK_DEADLINE_SECONDS` | 180 | 30–420؛ Render free بلا workers فلا استئناف بعد النوم |
| `AGENT_WORKERS` | 1 | خيط عمل واحد: 0.1 CPU وموديل مشترك |
| `AGENT_APPROVAL_TIMEOUT_SECONDS` | 600 | انتهائها يُلغي الطلب، لا ينفّذه |
| `AGENT_NETWORK_TOOLS` | 0 | 1 يفتح `web_fetch` (يبقى approval-first) |
| `AGENT_AUTO_APPROVE_READ_ONLY` | 0 | لا يشمل أبداً أدوات الشبكة |
| `AGENT_MEMORY_MAX_ITEMS` | 40 | ذاكرة لكل زائر، تُقرأ آخر 8 كسياق |
| `AGENT_MODEL` | من `runtime-config.json` | تجاوز نموذج الوكيل وحده |
| `AGENT_FAKE` | 0 | `1` بلا `GEMINI_API_KEY`: ردود ثابتة لتجربة الواجهة والـsmoke، لا نموذج ولا شبكة |

### وضع التنفيذ (R6): نفس الوكيل، جدولتان

`backend/agent/execution.py` هو المكان الوحيد الذي يترجم شكل النشر إلى قرار:

```text
app.py  ->  execution_policy()  ->  ExecutionMode  ->  Service(mode=...)  ->  Agent.run()
                                                        ├── queued  (Render: عامل في الخلفية)
                                                        └── inline  (Vercel / PromptQL: داخل الطلب)
```

الوكيل نفسه **لا يعرف أن Vercel موجود**: لا `os.environ` في `backend/agent/` سوى طبقة
الأعداد (باسم `AGENT_*` فقط)، ولا مقارنة باسم مضيف في أي موضع — يفحص ذلك
`tests/test_agent_no_platform_branching.py` على AST. الفارق المسموح الوحيد هو **الميزانية
والجدولة**: `inline` يُلغي سقفًا على `AGENT_MAX_STEPS`/`AGENT_MAX_AI_CALLS`/
`AGENT_TASK_DEADLINE_SECONDS`/`AGENT_PROVIDER_TIMEOUT_SECONDS` بـ`min()` (الأقل يفوز، لذا
`AGENT_MAX_STEPS=1` لا يُعاقَب)، ويرفض الأدوات التي تحتاج موافقة، ولا يقبل مهمة في الطابور.
**لم تُضف أي متغيرات `AGENT_SERVERLESS_*`**: العقد الحالي يعبّر عن الوضع بالحدود الموجودة.

الدليل `tests/test_agent_execution.py`: نفس نص السيناريو المُشغَّل على `FakeProvider` يمر
بالمسارين، والخطة والخطوات ونتائج الأدوات والحالة والأحداث والتقرير **متطابقة حقلًا حقلًا**
(يُستثنى الزمن ومعرّفات الصفوف). ومهمّة تتجاوز الميزانية تُنهى `failed` برمز خطأ واضح في
الوضعين، و`429` يفشل بلا تحويل إلى مزوّد آخر، و`submit()` على مضيف Serverless يرفض بدل أن
يُنتج دوّارًا لا ينتهي أبدًا.

### جرّبها محلياً بلا مفاتيح

```bash
cd backend
AGENT_FAKE=1 AGENT_NETWORK_TOOLS=1 WAHA_DB=/tmp/waha.db python app.py    # http://127.0.0.1:5210
# نافذة أخرى: تجرّب الـAPI والمساحة معاً
scripts/smoke.sh http://127.0.0.1:5210
# أو الواجهة الكاملة: ضع "api_base": "http://127.0.0.1:5210" في docs/data/config.json ثم
python -m http.server 8080 --directory docs    # http://localhost:8080 → مساحة العمل
```

`api_base` المحلي للتجربة فقط؛ لا ترفعه إلى المستودع لأن Pages تُخدم من أصل آخر.

## لوحة «مكوّنات النظام» في الواجهة

`docs/assets/components.js` (بلا DOM، ولهذا يستطيع node تشغيله) يحوّل ردّي `/health`
و`/api/agent/config` إلى ستّ بطاقات: نموذج اللغة · قاعدة البيانات · فهرس الاسترجاع · وضع التنفيذ ·
أدوات الوكيل · مصدر البحث. القاعدة التي تحرسها الاختبارات: **لا بطاقة تخضرّ إلا إذا وصلها
payload**؛ وما لم يُقرأ يُكتب «غير معروف» بلا لون يدّعي عملًا، وقبل وصول الرد تكون الحالة
«جارٍ القراءة…». على نسخة Pages بلا `api_base` تقول اللوحة «غير متصل» وتبقي البطاقات رمادية،
ولا تُطلق أي طلب شبكة إطلاقًا. اللوحة تعرض ما ينشره الخادم فقط — لا أسماء نماذج أو خدمات
لا يستخدمها هذا التطبيق. عقدها: `tests/browser_components.test.mjs` (15 فحصًا) +
`tests/test_components_contract.py`، وكلاهما في CI.

## تكاملات المالك · `/integrations`

صفحة إدارة واحدة خلف `/integrations`، تقرأ وتشغّل تكاملين فقط:

| التكامل | قراءة | كتابة |
|---|---|---|
| GitHub Actions | `GET /api/owner/integrations/github/runs` — سجل التشغيلات | `POST /api/owner/integrations/github/dispatch` — تشغيل workflow |
| Vercel | `GET /api/owner/integrations/vercel/deployments` — سجل النشرات | `POST /api/owner/integrations/vercel/deploy-hook` — نشر فعلي |

**الأسرار لا تغادر الخادم.** `GITHUB_*` و`VERCEL_*` و`DEPLOY_HOOK_*` تُقرأ في
`backend/integrations/config.py`، و`describe()` ينشر أسماء المتغيرات وحالاتها فقط، وما
يظهر في الواجهة بصمة SHA-256 رباعية الأطراف للرمز لا الرمز. وكل رسالة خطأ قادمة من
GitHub أو Vercel تمرّ بمُحمِّر قبل أن تصل إلى المتصفح، ومسار الخروج في مدقّق الطبقة
الحية يعيد فحص المحضر كاملًا قبل طباعته.

**الحماية:** جلسة موقّعة بـHMAC تنتهي بعد 8 ساعات · CSRF مرتبط بالجلسة · قائمة CORS
خاصة بالإدارة · Flask `TRUSTED_HOSTS` لمنع Host/DNS rebinding · فحص وحصر DNS لـDeploy Hook
ثم تثبيت عناوين IP في اتصال TLS · نص تأكيد لكل عملية كتابة · حدّ 3 عمليات كتابة في الدقيقة.
التفصيل والأسباب في `ARCHITECTURE.md` §7.

### التحقق على ثلاث طبقات

| الطبقة | الملف | ما تثبته |
|---|---|---|
| Unit | `tests/test_integrations_core.py` (56) | الإعداد والمُحمِّر وحراس النقل والعميلان، بنقل مُزيَّف — بما فيها DNS pinning وحالات 401/403/422/429/5xx |
| Integration | `tests/test_integrations_api.py` (36) | الحافة كلها عبر عميل Flask: Host موثوق ضد DNS rebinding، ومن يُسمح له، ومن أي مصدر، وبأي CSRF أو تأكيد |
| Live | `tests/test_integrations_live.py` (8) + `scripts/integrations_live_check.py` | أن **الرموز وصلاحياتها** تعمل فعلًا — وهو ما لا تراه الطبقتان السابقتان لأن الـmock يجيب 200 مهما كان الرمز |

الطبقة الحية معطّلة افتراضيًا ولا تعمل في CI:

```bash
# القراءتان فقط (بلا تغيير في الحالة)
GITHUB_TOKEN=… GITHUB_REPO=… VERCEL_TOKEN=… VERCEL_PROJECT_ID=… \
VERCEL_TEAM_ID=team_… \
  python scripts/integrations_live_check.py

# والعمليتان الكتابيتان — تبدآن تشغيل CI فعليًا ونشرًا إنتاجيًا فعليًا
python scripts/integrations_live_check.py --allow-mutations

# تشغيل الاختبارات الحية
WAHA_LIVE_INTEGRATIONS=1 python -m unittest tests.test_integrations_live

# إثبات المدقّق نفسه بلا شبكة وبلا أسرار (هذا ما يشغّله CI)
python scripts/integrations_live_check.py --self-test
```

العمليتان الكتابيتان خلف `--allow-mutations` عمدًا: «تحقق من رموزي» يجب ألا يكون أمرًا
مدمّرًا. ولا تُنفَّذان من أي اختبار، لأن مجموعة اختبارات لا يجوز أن تقرر نشر إنتاج نيابةً
عنك.

## النشر المستقل — Render + Neon + Gemini

خيار النشر المجاني بدون بطاقة (تحقق من الأسعار قبل الاعتماد عليها؛
تتغير بسرعة):

- **Render** (Web Service مجاني): 512 MB RAM و0.1 CPU، الخدمة تنام بعد
  15 دقيقة بلا طلبات، والقرص **مؤقت** — لذلك لا يمكن الاعتماد على SQLite
  المحلي، و5 GB bandwidth شهرياً (منذ أبريل 2026).
- **Neon** (Postgres): خطة مجانية دائمة بلا بطاقة (100 CU-ساعة شهرياً،
  0.5 GB تخزين، تنام بعد 5 دقائق خمول). اخترناها على Turso لأن قاعدة
  البيانات المجانية هناك قد تُؤرشف عند الخمول ولأن تكامل `DATABASE_URL`
  مع Render هو الأسلوب القياسي.
- **ملف `render.yaml`** في جذر المستودع يصف الخدمة كاملة (يمكن النشر منه
  مباشرة: New → Blueprint).

### متغيرات البيئة

| المتغير | القيمة | ملاحظات |
|---|---|---|
| `DATABASE_URL` | من لوحة Neon (رابط **pooled**) | أبقِ `sslmode=require`. إذا فشل الاتصال احذف `channel_binding=require` (قد لا يدعمه driver قديم)؛ والكود يعطّل prepared statements عبر `prepare_threshold=None` ليتوافق مع pgbouncer في وضع transaction |
| `GEMINI_API_KEY` | من Google AI Studio | لا تضعه في المستودع أبداً ولا في أي محادثة أو لقطة شاشة؛ إن انكشف فاستبدله (rotate) فوراً من AI Studio |
| `WAHA_SECRET` | **لا تضبطه يدويًا عند استخدام Blueprint** | `render.yaml` يعلن `generateValue: true` فيولّده Render مرة واحدة ويثبّته عبر النشرات؛ إدخاله يدويًا يتجاوز المولَّد أو يتعارض معه. خارجه (تشغيل محلي) يُنشأ ملف جانبي مؤقت يضيع مع كل نشر فتُبطَل جلسات الزوار |
| `WAHA_ALLOWED_ORIGINS` | `https://sayedelazameydesign-crypto.github.io` | قائمة CORS مفصولة بفواصل. القيمة **origin فقط بدون `/1pro`** لأن المتصفح يقارن الـorigin لا المسار؛ same-origin يبقى مسموحًا فقط إذا كان Host ضمن `TRUSTED_HOSTS` |
| `WAHA_MODEL` | اختياري | يتجاوز النموذج في `runtime-config.json` |
| `WAHA_RAG_DIR` | اختياري (`<repo>/data/rag`) | دليل فهرس R1 الذي يقرأه `GET /api/search`. يُضبط في اختبار أو نسخة بديلة فقط؛ مساره الخاطئ يرجع `503 rag_index_missing` ولا يُبدَّل بنتائج مُختَرَعة |
| `WAHA_OWNER_TOKEN` | سرّ تولّده أنت | يفتح `/integrations`. بلا قيمة تُرجع كل مسارات `/api/owner/*` ‏`503 not_configured` ولا يُقبل دخول أبدًا. ولّده بـ`openssl rand -hex 32` وضعه في Production فقط |
| `GITHUB_TOKEN` | من GitHub | ‏PAT أو fine-grained بصلاحية `actions: read` للقراءة، و`actions: write` لتشغيل workflow. يبقى في الخادم ولا يصل إلى المتصفح |
| `GITHUB_REPO` | `owner/name` | صيغة خاطئة تُرفض بـ`invalid_config` قبل أي نداء شبكة |
| `GITHUB_WORKFLOW_ID` | اسم الملف أو رقمه | بدونه تبقى القراءة متاحة وتُعطَّل كتابة `dispatch` وحدها |
| `VERCEL_TOKEN` | من Vercel | للقراءة فقط. لا يستطيع النشر: النشر يمرّ بالـhook |
| `VERCEL_PROJECT_ID` / `VERCEL_TEAM_ID` | من لوحة Vercel | ‏`TEAM_ID` **معرّف** لا اسم: ‏`team_…` و`prj_…`. الـslug أو قيمة قديمة تُرفض بـ`403 Not authorized` في كل نداء مقيّد فتبدو المشكلة في الرمز لا في المتغيّر؛ `deploy_doctor` وسكربت المزامنة يفحصان الشكل الآن |
| `DEPLOY_HOOK_URL` | من Vercel · Deploy Hooks | **الرابط نفسه هو السر**، فيُعامَل كسرّ: لا يُنشر ولا يُسجَّل. يقبل فقط HTTPS على `api.vercel.com`؛ تُفحص كل إجابات DNS كعناوين عامة ثم تُثبَّت في الاتصال مع إبقاء اسم المضيف للتحقق من TLS، فلا توجد نافذة DNS-rebinding بين الفحص والاتصال |
| `WAHA_OWNER_ALLOWED_ORIGINS` | اختياري | قائمة CORS **خاصة بصفحة الإدارة**، مستقلة عن `WAHA_ALLOWED_ORIGINS`. افتراضيًا: نفس المصدر فقط، لكن بعد قبول Host صريح من `TRUSTED_HOSTS` |
| `WAHA_TRUSTED_HOSTS` | اختياري على المنصة، مطلوب للمضيفات المخصصة وملف الفحص المحلي | أسماء مضيفين exact مفصولة بفواصل، بلا scheme أو port أو wildcard. Render يثق تلقائيًا بـ`RENDER_EXTERNAL_HOSTNAME`، وVercel بـ`VERCEL_URL` وأصل الإنتاج؛ أضف هنا النطاق المخصص. `deploy_doctor` يرفض نشرًا عامًا بلا مصدر host موثوق |
| `WAHA_OWNER_SESSION_TTL` | `28800` (8 ساعات) | يُحصر بين 300 و86400 ثانية |
| `WAHA_OWNER_WRITE_LIMIT_PER_MINUTE` | `3` | حدّ عمليات الكتابة، في عدّاد مستقل عن القراءة |
| `AGENT_*` | اختياري | ميزانيات طبقة الوكيل وحدودها؛ الجدول الكامل في «طبقة الوكيل». قبل النشر شغّل `python scripts/deploy_doctor.py --target render` |
| `PROMPTQL_PLATFORM_API_URL` + `WAHA_TRUST_PROMPTQL=1` | **وضع PromptQL فقط — لا تضبط أياً منهما في النشر المستقل** | ⚠️ مع `WAHA_TRUST_PROMPTQL=1` تُقرأ ترويسة `X-PromptQL-Visitor-Token` **بدون تحقق توقيع** (يُفحص `exp` و`sub` فقط، دالة `identity` في `backend/app.py`)، فأي زائر يستطيع تزوير الهوية وتجاوز cooldown/الحدود. كذلك `PROMPTQL_PLATFORM_API_URL` وحده يحوّل مسار AI إلى البوابة ويُلغي مسار `GEMINI_API_KEY`. في النشر المستقل: اترك المتغيرين غير مضبوطين |

أمر البدء: `gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 8 --timeout 90`
(المهلة 90 ثانية لأن طلب Gemini الواحد قد يستغرق حتى 75 ثانية، والخيوط لأن مساحة العمل
تفتح بث أحداث SSE؛ عامل sync واحد بلا خيوط يحبس بقية الطلبات خلفه).

### خطوات مختصرة (بهذا الترتيب)

1. **Neon**: أنشئ المشروع والقاعدة، وانسخ رابط **pooled** مع `sslmode=require`.
   `/readyz` بعد النشر هو الفحص الذي يكشف أي مشكلة في الرابط أو في الـdriver.
2. **Render Blueprint**: أنشئ الخدمة من `render.yaml`
   (`DATABASE_URL` + `GEMINI_API_KEY` + `WAHA_ALLOWED_ORIGINS`؛ بدون
   `WAHA_TRUST_PROMPTQL`؛ و`WAHA_SECRET` يولّده Render تلقائياً).
3. **Smoke للخادم**:

   ```bash
   python scripts/deploy_doctor.py --env-file service.env --target render   # قبل النشر
   scripts/smoke.sh https://<service>.onrender.com
   curl -sS --max-time 90 https://<service>.onrender.com/health
   curl -sS --max-time 90 -i https://<service>.onrender.com/readyz   # 204 بلا جسم
   ```

   يفحص السكربت: `/health` (بلا لمس قاعدة بيانات) و`/readyz` (يوقظ Neon ويثبت أن
   `initialize()` بنى المخطط)، وCORS من أصل Pages مع رفض أي أصل أجنبي، ودورة هوية كاملة
   (`register` ← `/api/me`)، و`/api/agent/config` حيث يجب أن تظهر **كتلة `execution`**
   (الوضع + الميزانية + `new_knobs: 0`)، ومهمة حقيقية حتى حالة طرفية مع قراءة سجل الأحداث،
   و**memory** كتابةً وقراءةً، و**artifact** المهمة قراءةً لمالكها، و`/api/search` أنه يُرجع
   `RAG_LOCAL` باستشهاد حرفي وأن سؤالًا خارج الكتالوج يُرجع **0 نتيجة** بـ`deferred`. على
   `inline` (Vercel) لا يُرجّ السكربت طابورًا: المهمة تنتهي داخل الطلب فيقرأ الحالة من سجل
   الأحداث ويُكمل. `ai_enabled=false` فشلٌ **متوقّع** على خادم بلا `GEMINI_API_KEY`؛
   و`SMOKE_TOKEN`/`SMOKE_CSRF` يعيدان استخدام زائر عند حدّ `register` (5/ساعة/IP).

   استخدم `--max-time 90` دائماً: أول طلب بعد خمول Render المجاني قد يستغرق
   ~50 ثانية. نجاح `/readyz` بـ204 يعني أن `initialize()` بنى الجداول على
   قاعدة Neon الفارغة.
4. **`docs/data/config.json`**: ضع عنوان الخادم في `api_base` ثم commit وPR
   (CI يشغّل فحص الفهرس والاختبارات و`node --check`).
5. **Pages يدوياً**: Actions → «Publish static skill catalog to Pages» →
   Run workflow، ثم فحص متصفح: أول زيارة بعد خمول تُظهر رسالة
   «جارٍ إيقاظ خادم واحة…» وتعيد المحاولة تلقائياً، ثم أنشئ جلسة وأرسل رسالة
   وجرّب التصدير والحذف.
6. بالتوازي أو بعده: PR صغير للثابت `DEFAULT_RETRY_AFTER_SECONDS` + سطر توثيق.
   هذا ليس شرطاً للنشر في وضع Gemini المستقل، لأن مسار NVIDIA (صاحب الحد
   الزمني) معطّل هناك بـ`503 nvidia_requires_gateway`.

### ملاحظات التشغيل المجاني

- `/health` **لا يلمس قاعدة البيانات عمداً** حتى لا يوقظ Neon كل بضع
  دقائق ويحرق ساعات الحوسبة المجانية؛ استخدمه لفحص الصحة، واستخدم
  `/readyz` للفحص العميق عند الحاجة فقط. و`/api/search` مثله: يقرأ ملفًا مودَعًا
  في المستودع، فلا يوقظ Neon ولا يمسّ رصيد ساعة الحوسبة.
- أول طلب بعد الخمول بطيء (استيقاظ Render ثم استيقاظ Neon). إن أردت
  تقليل ذلك، وجّه ping خارجياً (مثل UptimeRobot) إلى `/health` كل
  10–14 دقيقة — وليس إلى `/readyz`.
- واجهة `docs/` مهيأة لهذا الخمول: كل طلب خلفي بمهلة 80 ثانية، مع رسالة
  «جارٍ إيقاظ خادم واحة…» وإعادة محاولة تلقائية (3 مرات/5 ثوانٍ) قبل إعلان
  «الخادم غير متاح»؛ لا تظهر النتيجة «غير متاح» بسبب cold start عابر.
- فحص الاتصال من جهة الخادم: `/health` لا يلمس Neon، و`/readyz` هو الفحص
  العميق الوحيد. إن رجع `/readyz` بخطأ اتصال فراجع `DATABASE_URL`
  (`sslmode=require` أولاً، ثم احذف `channel_binding=require` إن لزم).
- حدود النشر المستقل: 5 تسجيلات جديدة/ساعة لكل IP، و30 طلب AI/ساعة لكل
  مستخدم، و120 طلب AI/ساعة لكل IP.
- البيانات تُحفظ في Postgres، ولا شيء مهم على قرص الخادم المؤقت.

### بديل النشر: Vercel (خطة Hobby)

`vercel.json` + `api/index.py` يشغّلان نفس تطبيق Flask كدالة Serverless واحدة.
Vercel يكتشف Flask (Backend Framework) فيوجّه **كل** مسار إلى التطبيق بصورته
الأصلية، فلا يحمل `vercel.json` أي `rewrites`. خطة Hobby مجانية **ولا تطلب بطاقة
ائتمانية**، فلا حاجة لأي «مسار التفاف» للنشر. انتبه إلى أن Hobby مشروطة بالاستخدام الشخصي غير التجاري في شروط
Vercel؛ المشروع التجاري يتطلب Pro.

- **مهام الوكيل تُنفَّذ داخل الطلب**: لا شيء يستمر بعد إرسال الرد على Serverless، لذلك
  يفرض `VERCEL=1` وضع `mode: inline` بسقف خطوة واحدة و45 ثانية و3 استدعاءات نموذج
  (تُرفض الأدوات التي تحتاج موافقة). الأرقام ليست في `app.py` بل في
  `SERVERLESS_CAPS` داخل `backend/agent/execution.py`، و`DEADLINE_SECONDS` (45) أقل من
  `maxDuration` (60) عمداً — تختبر ذلك `test_serverless_budget_fits_vercels_declared_ceiling`.
  المهام الأطول تحتاج Render؛ لا متغير `AGENT_SERVERLESS_*`.
- **`maxDuration: 60`**: هذا السقف مضمون على Hobby. القيمة 300 لا تُقبل إلا
  حيث يكون Fluid Compute مفعلاً، وقد تُرفض في المشاريع الأقدم.
- **لا `rewrites` إلى `/api/index`** — ولا أي كتابة شاملة. في مشاريع Backend Framework
  على Vercel تُسلّم الكتابة الداخلية التطبيق **مسار الوجهة** لا المسار الأصلي، فقد جعلت
  `/(.*) → /api/index` يرى Flask `PATH_INFO=/api/index` في كل طلب ويردّ صفحته 404 على `/`
  و`/health` و`/readyz` وكل `/api/*` (لوحظ حيًّا على `cela-umber.vercel.app`). بناء Flask
  يوجّه كل المسارات بنفسه، والاختبار `tests/test_vercel_wrapper.py` ومعالج النشر
  `deploy_doctor.py --target vercel` يفشلان إن عادت الكتابة.
- **لا تجمع `builds` مع `functions`** في `vercel.json`: الاثنان متعارضان
  ويفشل البناء برسالة «Conflicting functions and builds». كذلك `excludeFiles`
  صالح **داخل** `functions` فقط وليس في جذر الملف.
- **`requirements.txt` في الجذر مسطّح عن قصد**: Vercel يقرأ هذا الملف بمحلّله الخاص،
  ولا يفهم سطر `-r backend/requirements.txt` — يفشل البناء بـ
  `could not parse requirements.txt: Error parsing included file`. لذلك القائمة مكتوبة
  كاملة في الجذر، ويحرس تطابقها مع `backend/requirements.txt` اختبار
  (`tests/test_vercel_wrapper.py::RootManifestTests`) حتى لا تنحرف نسختان بصمت.
- **`/tmp` وحده قابل للكتابة**: `backend/app.py` يحوّل مسار SQLite وملف
  `csrf.secret` إلى `/tmp` عند وجود المتغير `VERCEL`. لكن `/tmp` زائل ويُعاد
  تدويره، **لذلك `WAHA_SECRET` يجب أن يكون مضبوطاً في Production** وإلا
  بُطلت جلسات الزوار مع كل إعادة تدوير.
- **متغيرات Production فقط**: `DATABASE_URL` و`GEMINI_API_KEY` و`WAHA_SECRET`
  و`WAHA_ALLOWED_ORIGINS`. **يُمنع** ضبط `WAHA_TRUST_PROMPTQL` أو
  `PROMPTQL_PLATFORM_API_URL` في Vercel — الأول يجعل ترويسة الهوية تُقبل
  **بدون تحقق توقيع** (انتحال هوية وتجاوز للحدود)، والثاني يحوّل مسار AI إلى
  البوابة ويُلغي مسار `GEMINI_API_KEY`.
- **Deployment Protection**: أبقِ **Standard Protection** مفعلاً. لا تختر
  «All Deployments» حتى تبقى روابط المعاينة محمية دون كسر Production.
- **اتصالات Neon**: نمط Serverless يفتح اتصالاً جديداً مع كل استدعاء بارد؛
  راقب Neon Console تحسباً لاستنفاد الاتصالات واستخدم رابط **pooled**.
- **نقل المفاتيح إلى Vercel**: Vercel لا يقرأ أسرار GitHub، فالوسيط هو workflow
  **Sync production keys to Vercel** مع `scripts/vercel_env_sync.sh`: يقرأ الأسرار
  على المُشغِّل (المكان الوحيد الذي تُكشف فيه قيمها)، ويتحقق من كل قيمة باختبار
  حقيقي، ويكتبها على مشروع Vercel، ثم يعيد النشر ويفحص الحيّ. لا تُطبع أي قيمة
  أبدًا. يحتاج `VERCEL_TOKEN` إلى جانب `DATABASE_URL` و`GEMINI_API_KEY`
  و`WAHA_SECRET`؛ وإن غاب `DATABASE_URL` أو لم يكن رابط Postgres (لصقُ مفتاح
  `napi_…` مكانه خطأ وقع فعلًا)، يبني السكربت الرابط المُجمَّع `-pooler` من
  `NEON_API_KEY` ويختبره بـ`SELECT 1` قبل الكتابة — فلا يُكتب شيء إلى GitHub.
  التفاصيل في `DEPLOY-VERCEL.md` → «4) المفاتيح».

## خادم PromptQL

`backend/app.py` يتوقع أن يكون الوصول عبر بوابة PromptQL الموثوقة، وأن
تزوده بهوية الزائر وتوكن محدود لكل طلب. لا يتحقق التطبيق من توقيع أي
توكن عشوائي بنفسه: عند `WAHA_TRUST_PROMPTQL=1` تُقرأ ترويسة
`X-PromptQL-Visitor-Token` بفك الـpayload والتحقق من `exp` و`sub` فقط، بلا
أي تحقق توقيع (دالة `identity`). لذلك أي زائر قادر على تزوير `sub` وتجاوز
حدود التطبيق، و**لا يجوز تفعيل هذا العلم على خدمة عامة**؛
**لا تعرض التطبيق مباشرة للإنترنت ولا تقبل ترويسة الهوية من متصفح غير موثوق**.
عند الإقلاع يطبع التطبيق تحذيراً في السجل إذا كان العلم مفعّلاً.

إعداد الخدمة يحتاج عنوان Platform API الصحيح من بيئة PromptQL،
وموافقة الزائر على التكامل `__gemini-web-search`. المثال في
`runtime-config.json` غير سري، لكن اسم النموذج وتوفره قد يتغيران.

```bash
cd backend
# داخل البيئة الموثوقة، أو للمعاينة العامة المعطلة فقط:
python app.py
```

خارج PromptQL يظهر الكتالوج فقط، ولا تعمل المحادثات والحفظ.
البيانات محفوظة في `backend/data/waha.db`، ولكل زائر سجلاته.
حد التطبيق الحالي 30 طلب AI/ساعة لكل مستخدم، مع حد المزوّد الإضافي.
هذا الحفظ ليس نسخة احتياطية ولا بديلاً لنظام مصادقة مستقل.

## ما أُنجز وما يحتاج إعداداً

- [x] نقل نسخة من التطبيق الحالي بدون البيانات أو الأسرار.
- [x] ست مهارات عربية وفهرس وملفات تنزيل مولدة.
- [x] واجهة Pages ثابتة تُظهر الحدود بوضوح.
- [x] اختبارات محلية وWorkflows جاهزة، ونشر Pages الفعلي تم يدوياً.
- [x] قوالب Issues لبلاغات المشاكل وطلبات المهارات.
- [x] رفع الملفات، وCI يعمل على كل push وPR.
- [x] دعم الوضع المستقل للخادم: هوية يصدرها الخادم، CSRF، CORS محصورة،
      محوّل Postgres (مع بقاء SQLite للتشغيل المحلي والاختبارات)، اتصال
      مباشر بـ Gemini عبر `GEMINI_API_KEY`، وواجهة `docs/` قابلة للربط
      بعنوان الخادم عبر `docs/data/config.json`.
- [x] طبقة الوكيل: حلقة plan/act/observe/reflect/report مع حالة محفوظة، موافقات،
      artifacts، ذاكرة، SSE، ومحوّل Provider لا يعرف Flask ولا مزوّداً بعينه.
- [x] `scripts/deploy_doctor.py`: فحص بيئة النشر بلا شبكة وبلا طباعة مفاتيح (+self-test في CI).
- [x] إعلان الإصدار `v0.1.0` على `main`.
- [x] طبقات الاسترجاع والتقييم وعقد النشر (PR #8، دُمجت عند `9a7af62`): **R1** فهرس حتمي
      بمصدر وبلا فقد · **R2** `GET /api/search` فوق فهرس R1 **كما هو** + تسمية المتصفح
      لمصدره (`RAG_LOCAL`/`BROWSER_FALLBACK`) · **R3** `kb_search` داخل حلقة الوكيل عبر R2
      وحده (5 نتائج، 3000 حرف أدلة، استشهاد حرفي، `ToolError` عند فهرس مفقود) ·
      **R4** `eval/train.json` + `scripts/rag_eval.py` يقيسان ويعملان بوابة CI ·
      **R6** `ExecutionMode {queued,inline}` سياسةً في `backend/agent/execution.py`،
      وبلا متغيرات مضيف جديدة.
- [x] **R5 (المتجهات) مُقفَلة بالقياس لا بالرأي:** `recall@5 = 0.9375 ≥ 0.80` على 6 مهارات
      و2882 بايت و`pdf_count = 0` ⇒ `vector_enabled: false`. تُفتح عند تخطّي حدّ مقيس.
- [ ] إعادة نشر Pages بنشرة واحدة (آخر نشر سابق على `docs/assets/app.js` و`config.json`
      الحاليَّين): Actions → «Publish static skill catalog to Pages» → Run workflow.
- [x] نشر الخادم فعلياً: Vercel يخدم `backend/app.py` على `https://cela-umber.vercel.app`
      من `main` (`72371d6`)، و`api_base` مضبوط عليه.
- [ ] ضبط مفاتيح الإنتاج على Vercel — `DATABASE_URL` (Neon) و`GEMINI_API_KEY`
      و`WAHA_SECRET` و`WAHA_ALLOWED_ORIGINS` — عبر `scripts/vercel_env_sync.sh`. حتى
      ذلك الحين النشر يعمل بـSQLite زائل في `/tmp` وبلا نموذج (`ai: disabled`).
- [ ] تفعيل Discussions أو إعداد نطاق مخصص.
- [x] اختيار ترخيص Apache-2.0 للكود (راجع `LICENSE`).
- [ ] Streaming للمحادثة نفسها، أدوات أكثر، ومهام أطول من عمر الطلب — كلها تحتاج
      طبقة تنفيذ خارج الخطة المجانية (انظر خارطة الطريق في `ARCHITECTURE.md`).

لا توجد مزامنة حية مع GitHub داخل التطبيق الحالي؛ مكتبة المهارات هنا
تُبنى من الملفات المثبتة في الإصدار. يمكن إضافة مزامنة مُراجعة لاحقاً.

حصص GitHub والاستضافة وAI تعتمد على الخدمة والخطة والاستخدام. لا نفترض أن
كل الخدمات مجانية بلا حدود، ولا نضمن استضافة backend مجانية دائمة.

## التراخيص والخصوصية

كود هذا المشروع مرخّص بموجب **Apache License, Version 2.0**؛ راجع ملف
`LICENSE` للنص الكامل. خطوط Noto Sans Arabic لها ترخيص منفصل مرفق في
`FONT-LICENSE.txt`. لا تُضمّن وثيقة Google أو المحادثات أو بيانات
المراجعين أو أسرار الخدمة في المستودع.

هذا القالب مُعدّ لهذا المشروع ولا يدّعي أنه SDK رسمي لـPromptQL.

## NVIDIA — اختيار إضافي للجلسات

- يحتاج التطبيق الكامل اتصال NVIDIA شخصياً باسم `waha-nvidia`؛ لا يستخدِم مفتاح Gemini.
- النموذج: `nvidia/nemotron-3.5-lightning-30b-a3b`، بإجابات حتى 512 رمزاً.
- عند إنشاء جلسة اختر Gemini أو NVIDIA؛ لا تغيير تلقائي، والمحادثات السابقة تبقى Gemini.
- يؤكد المستخدم مراجعة أهلية النقطة التطويرية المجانية وشروط حسابه قبل بدء جلسة NVIDIA.
- سقف NVIDIA في هذا التطبيق كله، عبر جميع المستخدمين: 10 محاولات/دقيقة و100 خلال 24 ساعة، وطلبان متزامنان كحد أقصى. يشمل العد المحاولات الفاشلة.
- سقف عام إضافي: 30 محاولة في الساعة لكل مستخدم. عند 429/402 من NVIDIA يُحفظ `Retry-After` كاملاً على صف الزائر، ويُكتب أيضاً صف حماية عام قصير للتطبيق كله بسقف يقارب خمس دقائق، دون إعادة محاولة أو تبديل موفّر تلقائي.
- إن جاء 429/402 بلا ترويسة `Retry-After` صالحة يُستخدم الافتراضي `DEFAULT_RETRY_AFTER_SECONDS = 60` ثانية (ثابت واحد في `backend/app.py` بدل رقم مبعثر)، ويُطبَّق على مسار Gemini أيضاً، مع سقف `AI_RETRY_AFTER_MAX_SECONDS` = 24 ساعة.
- ليست هذه الحدود ضماناً لحصة حساب NVIDIA أو مجانية إنتاجية دائمة.
- يطلب App Artifact موافقة الزائر على التكاملين `__gemini-web-search` و`waha-nvidia`، ويستخدم رمز الزائر لكل طلب. لا تخزن مفاتيح في الكود.
- تبقى GitHub Pages كتالوجاً ثابتاً بدون AI؛ الربط الخلفي يحتاج بيئة PromptQL أو الوضع المستقل الموصوف في «النشر المستقل».
- الوضع المستقل (خارج PromptQL) يدعم Gemini مباشرة عبر مفتاح API؛ جلسات NVIDIA تتطلب بوابة PromptQL واتصال الزائر الشخصي، وتُرفض برسالة واضحة.
