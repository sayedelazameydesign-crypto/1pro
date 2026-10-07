# النشر على Vercel (خطة Hobby) + Neon — بلا بطاقة ائتمان

دليل تشغيل مختصر. الشرح الكامل والقيود في `README.md` → «بديل النشر: Vercel (خطة Hobby)»،
والقرار المعماري في `ARCHITECTURE.md` → «النشر على المجاني». هذا الملف لا يكرّرهما، بل يجمع
الخطوات في مكان واحد.

## ما تحصل عليه فعلاً (بلا بطاقة)

| البند | القيمة |
|---|---|
| السعر | Hobby = 0$ دائمًا، **بلا بطاقة ائتمان**، والحساب لا يُفوتر أصلاً |
| الشرط | **شخصي/غير تجاري فقط**؛ أي استخدام تجاري يحتاج Pro (20$/مقعد) |
| عند التجاوز | تُوقف الميزة حتى نافذة الـ30 يومًا التالية — لا فاتورة مفاجئة |
| الحدود | 100GB نقل · 1M استدعاء دالة · 1M طلب edge · 100 نشر/يوم |
| مدة الدالة | `maxDuration: 60` في `vercel.json` (مضمون على Hobby بدون Fluid Compute) |
| مهام الوكيل | **inline**: خطوة واحدة · 45 ثانية · 3 استدعاءات نموذج، والأدوات التي تحتاج موافقة تُرفض |

> إن احتجت مهامًا أطول لاحقًا (خطوات متعددة، موافقات، مهام حتى 180 ثانية) فالمسار هو
> **Render** — وهو أيضًا بلا بطاقة — لأن `render.yaml` يشغّل `queued` بعامل خلفي.

## 1) Neon (قاعدة البيانات)

1. أنشئ مشروعًا على [neon.com](https://neon.com) — لا بطاقة.
2. انسخ رابط الاتصال **pooled** (يحتوي `-pooler`) وأبقِ `sslmode=require`.
3. إن فشل الاتصال لاحقًا: احذف `channel_binding=require` من الرابط.

## 2) Vercel (الخادم)

1. New Project → Import Git Repository → اختر `1pro`.
2. **Framework Preset: Flask** (المكتشف تلقائيًا — لا تختر Other)، و**Root Directory: جذر
   المستودع** (لا تغيّره): `vercel.json` في الجذر، والدالة في `api/index.py`، وبناء Flask
   يوجّه **كل** مسار إلى التطبيق بصورته الأصلية بلا أي `rewrites`.
3. لا تعدّل Build Command أو Output Directory. الملف يضبط كل شيء.

> **`requirements.txt` في الجذر مسطّح عن قصد.** Vercel يقرأه بمحلّل لا يفهم `-r`، وإن
> وجد سطر include يفشل البناء بـ`could not parse requirements.txt: Error parsing
> included file`. النسخة المسطّحة تُطابق `backend/requirements.txt` بايتًا ببايت في
> الحزم المثبَّتة، ويحرس التطابق اختبار في CI.

### لا `rewrites` في هذا المستودع — ولا تُعِدها

كان `vercel.json` يحمل `"rewrites": [{"source": "/(.*)", "destination": "/api/index"}]`،
وهذه الكتابة 404ت الموقع كله: في مشاريع Backend Framework على Vercel صارت الكتابة
الداخلية تُسلّم التطبيق **مسار الوجهة** لا المسار الأصلي، فرأى Flask `PATH_INFO=/api/index`
في كل طلب وردّ صفحته 404 على `/` و`/health` و`/readyz` وكل `/api/*` (لوحظ حيًّا على
`cela-umber.vercel.app`). بناء Flask يوجّه كل المسارات بنفسه
([توثيق Flask على Vercel](https://vercel.com/docs/frameworks/backend/flask): لا حاجة إلى
redirects في `vercel.json` ولا إلى مجلد `/api` أصلًا)، فحُذفت الكتابة.

الحراسة ثلاثية: `tests/test_vercel_wrapper.py` يرفض عودتها، و`deploy_doctor.py --target
vercel` يعتبر الكتابة إلى `/api/index` **خطأً** لا تحذيرًا، وسجل بناء سليم لا يحمل
`Internal rewrites in backend framework projects…`.

### متغيرات البيئة (Production فقط)

| المتغير | القيمة | ملاحظة |
|---|---|---|
| `DATABASE_URL` | رابط Neon الـpooled | ليس اختياريًا: `/tmp` وحده قابل للكتابة على Vercel، فـSQLite يضيع مع كل إعادة تدوير |
| `GEMINI_API_KEY` | من Google AI Studio | لا يوضع في المستودع ولا في المحادثة؛ عند أي انكشاف استبدله فورًا |
| `WAHA_SECRET` | **اضبطه يدويًا هنا** | بخلاف Render (حيث يولّده `render.yaml`)، بدونه تُبطَل جلسات الزوار مع كل إعادة تشغيل |
| `WAHA_ALLOWED_ORIGINS` | `https://sayedelazameydesign-crypto.github.io` | origin فقط بدون `/1pro`؛ المتصفح يقارن الأصل لا المسار |

**يُمنع** ضبط أيٍّ من: `WAHA_TRUST_PROMPTQL` (يقبل ترويسة هوية بلا تحقّق توقيع على خدمة عامة)
و`PROMPTQL_PLATFORM_API_URL` (يحوّل مسار AI إلى البوابة ويُلغي مسار المفتاح المباشر).
`VERCEL` يضبطها المنصّ نفسه تلقائيًا.

## 3) التحقق قبل النشر وبعده

```bash
# قبل: صحة البيئة (بلا شبكة، وبلا طباعة أي مفتاح)
python scripts/deploy_doctor.py --env-file service.env --target vercel

# بعد أول نشر: سموك كامل يفحص R1→R6 على الخادم الحيّ
scripts/smoke.sh https://<app>.vercel.app
curl -sS --max-time 60 -o /dev/null -w '%{http_code}\n' https://<app>.vercel.app/   # 200 واجهة Flask
curl -sS --max-time 60 https://<app>.vercel.app/health
curl -sS --max-time 60 -i https://<app>.vercel.app/readyz     # 204 بلا جسم
```

- `/` رجع **404 من Flask** (Not Found بصفحة Werkzeug)؟ عاد `rewrites` — احذفه.
- `/` رجع **404 من Vercel** (`404: NOT_FOUND`)؟ المشروع ليس في وضع Flask: بدّل
  Framework Preset إلى Flask ثم أعد النشر.

على `inline` لا يرجّ سكربت السموك الطابور: المهمة تنتهي داخل الطلب، فيقرأ الحالة من سجل الأحداث.
`ai_enabled=false` فشل متوقّع إن لم يُضبط المفتاح.

## 4) المفاتيح: من أسرار GitHub إلى Vercel

Vercel لا يقرأ أسرار GitHub، فالنقل يحتاج وسيطًا يعمل في مكان يرى فيه الاثنان.
الوسيط هو `scripts/vercel_env_sync.sh` عبر workflow **Sync production keys to Vercel**
(تشغيل يدوي: Actions → Run workflow). يفعل أربعة أشياء بالترتيب:

1. يقرأ الأسرار من بيئة المُشغِّل (المكان الوحيد الذي تُكشف فيه قيم الأسرار).
2. يتحقق من كل قيمة فعليًا: اتصال PostgreSQL حقيقي بـ`SELECT 1`، وطلب حقيقي إلى
   Google يثبت أن مفتاح Gemini مقبول، وفحص طول `WAHA_SECRET`، وأن
   `WAHA_ALLOWED_ORIGINS` يحتوي أصل Pages.
3. يكتب المتغيّرات على مشروع Vercel (Production) ويكرّر الكتابة بأمان (upsert).
4. يعيد نشر الإنتاج ثم يفحص الحيّ: `/health` (`ai:gemini` و`database:postgres`)،
   و`/readyz`، وترويسة CORS لأصل Pages.

**لا تُطبع أي قيمة أبدًا**: كل سطر يمرّ عبر مُنقِّح يحوّل القيمة إلى `***`، والقيمة
تُقرأ من متغيّر البيئة لا من وسيط سطر أوامر. الأسماء التي يبحث عنها، بالترتيب:

| المتغيّر | الأسماء المقبولة | لماذا |
|---|---|---|
| رابط Neon | `DATABASE_URL` ثم `NEON_DATABASE_URL` · `POSTGRES_URL` · `DB_URL` … (15 اسمًا) | بدونه يبقى SQLite على `/tmp` وتضيع الجلسات |
| مفتاح Gemini | `GEMINI_API_KEY` ثم `GOOGLE_API_KEY` · `GEMINI_KEY` … | بدونه `ai:disabled` والوكيل يرفض كل مهمة |
| سر الجلسات | `WAHA_SECRET` أو `WAHA_APP_SECRET` | بدونه يبطل Vercel جلسات الزوار عند كل إعادة تدوير |
| أصل الصفحة | `WAHA_ALLOWED_ORIGINS` أو `ALLOWED_ORIGINS` | وإن غابا يُستخدم أصل Pages افتراضيًا |
| رمز Vercel | `VERCEL_TOKEN` أو `VERCEL_API_TOKEN` | **إلزامي للكتابة على Vercel**؛ بدونه يتوقف السكربت بخطوة واضحة ولا يلمس Vercel |

### إضافة `VERCEL_TOKEN` (مرة واحدة)

1. [vercel.com/account/tokens](https://vercel.com/account/tokens) → Create Token →
   النطاق (Scope) = الفريق المالك للمشروع (`celia-fashion's projects`) → أنشئه.
2. GitHub → المستودع → Settings → Secrets and variables → Actions →
   New repository secret → الاسم **`VERCEL_TOKEN`** حرفيًا → الصق الرمز.
3. Actions → **Sync production keys to Vercel** → Run workflow.

> بديل بلا رمز: الصق القيم بنفسك في Vercel → Project → Settings → Environment
> Variables (Production) ثم اعمل Redeploy. الأول أصحّ لأن GitHub يبقى مصدر الحقيقة
> الوحيد، ولأن السكربت يتحقق من كل قيمة قبل أن تصل الإنتاج.

## 5) ربط الواجهة

1. ضع عنوان Vercel في `docs/data/config.json` → `api_base` (بدون مسار).
2. commit + PR → CI (فهرس + اختبارات + `node --check`) → merge.
3. Actions → «Publish static skill catalog to Pages» → Run workflow (نشر Pages **يدوي** عمدًا).
4. افتح الصفحة: مساحة العمل → لوحة «مكوّنات النظام» يجب أن تعرض `inline` وبطاقات حيّة من
   `/health` و`/api/agent/config`، وبطاقة «قاعدة البيانات: Postgres (Neon)».

## 6) سلوك الإجابة على هذا النشر: كتابة تدريجية وبطاقات مصادر

الردّ في المحادثة يُكتب تدريجيًا أمام الزائر بدل الظهور دفعة واحدة، ثم تُعلَّق تحته
حتى 3 بطاقات مصادر من فهرس R1 عبر `GET /api/search`. ما يهمّك كناشر على Hobby:

- **بلا كلفة دالة إضافية تُذكر**: الكتابة التدريجية تعمل في المتصفح فقط (الردّ محفوظ
  في قاعدة البيانات قبل بدء الكتابة)، والبطاقات طلب `GET` واحد للقراءة فقط —
  بلا استدعاء نموذج وبلا كتابة في القاعدة — فيبقى ضمن حدود Hobby نفسها.
- **يعمل على `inline` كما هو**: لا يعتمد على الطابور أو الموافقات أو مدة الدالة؛
  إن ظهر الردّ ظهرت الكتابة والبطاقات معه على Vercel وRender وPages المرتبطة بخادم.
- **البطاقات «مقاطع ذات صلة» لا توثيق للردّ**: ردّ المحادثة يأتي من النموذج، والبطاقات
  مقاطع قريبة من الفهرس للاستكشاف فقط، ولا تُبنى بطاقة إلا من صف `RAG_LOCAL` يحمل
  استشهادًا حرفيًا. عند غياب الفهرس تظهر ملاحظة صامتة بدل بطاقات مخترعة.
- **تقليل الحركة محترم**: من فعّل `prefers-reduced-motion` يرى الردّ كاملًا فورًا،
  والنقر على فقاعة تُكتب يُكملها فورًا.

### التحقق على الإنتاج

1. `scripts/smoke.sh https://<app>.vercel.app` يغطي `/api/search` أصلًا (نتيجة
   `RAG_LOCAL` باستشهاد + سؤال خارج الكتالوج بلا اختراع) — إن اخضرّ فمصدر البطاقات سليم.
2. يدويًا: ابدأ جلسة من الواجهة (`/` على Vercel أو Pages المرتبطة بخادم)، اسأل سؤالًا
   من مواضيع المهارات الست، وراقب: الردّ يُكتب تدريجيًا، ثم بطاقات بعناوين واستشهادات
   وزر «افتح المهارة».
3. رأيت «فهرس الاسترجاع غير متاح على هذا الخادم»؟ ملفات `data/rag/` غير منشورة مع
   الدالة — أعد النشر من فرع يحملها (`data/` غير مستبعدة في `excludeFiles` عمدًا).

## ما لا يفعله هذا النشر

- لا يشغّل مهامًا طويلة أو بانتظار موافقة (اقرأ القيود أعلاه قبل أن تَعِد أحدًا بها).
- لا يستخدم NVIDIA: جلسات NVIDIA تتطلب بوابة PromptQL وتُرفض بـ`503 nvidia_requires_gateway`.
- لا يجعل Hobby مناسبًا لاستخدام تجاري؛ الشروط تنص على الاستخدام الشخصي.
