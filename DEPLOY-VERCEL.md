# النشر على Vercel (خطة Hobby) + Neon — مشروط بالتحقق من عدم طلب بطاقة

دليل تشغيل مختصر. الشرح الكامل والقيود في `README.md` → «بديل النشر: Vercel (خطة Hobby)»،
والقرار المعماري في `ARCHITECTURE.md` → «النشر على المجاني». هذا الملف لا يكرّرهما، بل يجمع
الخطوات في مكان واحد.

## شرط المتابعة: التحقق من اللوحة

حالة GitHub/Vercel `Production: success` تعني اكتمال النشر المسجّل فقط؛ لا تثبت عمل
الموقع ولا غياب مطالبة بطاقة أو خطة مدفوعة في الحساب الحالي.

- **لم يُتحقق بعد:** لا تبدأ نشرًا جديدًا، ولا تشغّل workflow مزامنة المفاتيح؛ فهو يعيد النشر.
- **لا مطالبة بطاقة:** اضبط `DATABASE_URL` و`GEMINI_API_KEY` و`WAHA_ALLOWED_ORIGINS`
  و`WAHA_SECRET` من Settings → Environment Variables → Production، دون وضع القيم في
  الدردشة أو المستودع. بعدها فقط تابع النشر والتحقق من Pages.
- **ظهرت مطالبة بطاقة/خطة مدفوعة:** أوقف مسار Vercel؛ لا تضف بطاقة ولا ترقِّ الخطة.
  قيّم Cloudflare كبديل قبل أي انتقال. Pages يمكن أن تستضيف الواجهة الساكنة، لكن نقل
  خادم Flask الحالي يحتاج إثبات توافق runtime أو تكييفه؛ تحقق من دعم Flask/WSGI
  و`psycopg` واتصال Neon، ومن حدود الزمن والتخزين المؤقت والمصادقة وCORS.
  لا تعتبر نقل ملفات الواجهة نشرًا لخادم المحادثة، ولا تدّعِ أن البديل بلا بطاقة قبل التحقق.

لم تُؤكَّد حالة الفوترة لهذا الحساب من اللوحة. وصف الخطة أدناه ليس تأكيدًا لها.
المسار المستقل يستخدم Gemini؛ NVIDIA متاح عبر PromptQL فقط.

## الخطة المستهدفة (تُراجع شروطها قبل الاعتماد)

| البند | القيمة |
|---|---|
| السعر المستهدف | Hobby = 0$؛ تأكد من اللوحة أن المشروع لا يطلب بطاقة أو خطة مدفوعة قبل المتابعة |
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
| `WAHA_TRUSTED_HOSTS` | `cela-umber.vercel.app` أو نطاقاتك المخصصة | أسماء مضيفين exact مفصولة بفواصل، بلا scheme أو port أو wildcard. Vercel يضيف `VERCEL_URL` وأصل الإنتاج تلقائيًا؛ أضف كل alias مخصص هنا. أدرج اسم الإنتاج في `service.env` ليعرفه `deploy_doctor` محليًا |
| `WAHA_OWNER_ALLOWED_ORIGINS` | اختياري، مثلاً `https://cela-umber.vercel.app` | CORS خاص بـ`/integrations`, منفصل عن Origin الزائر. نفس المصدر يبقى مسموحًا فقط على Host اجتاز `TRUSTED_HOSTS` |

**DNS/Host hardening:** يرفض Flask أي Host خارج `WAHA_TRUSTED_HOSTS` أو اسم المنصة (`VERCEL_URL`/`VERCEL_PROJECT_PRODUCTION_URL`) قبل تشغيل أي route؛ لهذا لا يستطيع نطاق attacker أن يصبح «same-origin» عبر DNS rebinding. Deploy Hook يقبل `api.vercel.com` فقط، ويثبت IP العام الذي حُلّ في الفحص داخل اتصال TLS نفسه مع التحقق من الشهادة باسم المضيف الأصلي.

**يُمنع** ضبط أيٍّ من: `WAHA_TRUST_PROMPTQL` (يقبل ترويسة هوية بلا تحقّق توقيع على خدمة عامة)
و`PROMPTQL_PLATFORM_API_URL` (يحوّل مسار AI إلى البوابة ويُلغي مسار المفتاح المباشر).
`VERCEL` يضبطها المنصّ نفسه تلقائيًا.

## 3) التحقق قبل النشر وبعده

```bash
# قبل: صحة البيئة (بلا شبكة، وبلا طباعة أي مفتاح)
python scripts/deploy_doctor.py --env-file service.env --target vercel

# بعد نشر مسموح: استخدم نطاق الإنتاج الثابت من Settings → Domains
# فحص CORS فقط: لا يستدعي Gemini ولا يحتاج Bearer فعليًا في OPTIONS
curl -sS --max-time 60 -i -X OPTIONS \
  -H 'Origin: https://sayedelazameydesign-crypto.github.io' \
  -H 'Access-Control-Request-Method: POST' \
  -H 'Access-Control-Request-Headers: authorization,content-type,x-waha-csrf' \
  https://<app>.vercel.app/api/sessions

# السموك الكامل يفحص R1→R6، يكتب بيانات اختبار وقد يستهلك حصة Gemini
scripts/smoke.sh https://<app>.vercel.app
curl -sS --max-time 60 -o /dev/null -w '%{http_code}\n' https://<app>.vercel.app/   # 200 واجهة Flask
curl -sS --max-time 60 https://<app>.vercel.app/health
curl -sS --max-time 60 -i https://<app>.vercel.app/readyz     # 204 بلا جسم
```

فحص CORS ينجح عند `204` مع الأصل مطابقًا تمامًا، والسماح بـ`POST` وبالترويسات الثلاث
`Authorization` و`Content-Type` و`X-Waha-CSRF`. المصادقة Bearer + CSRF، لا كوكيز.

رابط النشر المحدد مثل `cela-9wxj9z9ae-…vercel.app` ليس نطاق الإنتاج الثابت؛ قد تحجبه
Deployment Protection. إذا حصلت على `401` من المنصة، راجع Settings → Deployment
Protection ونطاق الإنتاج قبل تشخيص عطل Flask. لا تعطّل حماية الروابط الخاصة تلقائيًا؛
يجب أن يكون API المقصود لواجهة Pages العامة متاحًا لها دون شاشة دخول Vercel.

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
2. يتحقق من كل قيمة فعليًا: اتصال PostgreSQL حقيقي بـ`SELECT 1` (بثلاث محاولات، ويفرّق
   بين قيمة رفضها الخادم وشبكة متعثّرة: الأولى عيب يُسمّى، والثانية «لم أتحقق» ولا يبنى
   عليها تغيير سرّ)، وطلب حقيقي إلى Google يثبت أن مفتاح Gemini مقبول، وفحص طول
   `WAHA_SECRET`، وأن `WAHA_ALLOWED_ORIGINS` يحتوي أصل Pages. ثم — قبل أي كتابة — **يحلّ الفريق والمشروع**
   ويسأل `/v2/teams` عمّا يراه الرمز فعلًا، فلا تمرّ قيمة `VERCEL_TEAM_ID` خاطئة إلى
   نداء مقيّد (التفصيل أدناه).
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

### مفتاح Neon بديلًا عن لصق الرابط: `NEON_API_KEY`

لصق الرابط يدويًا عرضة لخطأ حقيقي وقع فعلًا في هذا المستودع: مفتاح Neon API
(`napi_…`) وُضع في خانة `DATABASE_URL`. عند وجود `NEON_API_KEY` لم يعد ذلك خطأً قاتلًا:

1. إن كان `DATABASE_URL` **غائبًا أو ليس رابط Postgres** (طوله يُذكر ولا تُطبع قيمته)،
   يبني السكربت الرابط بنفسه من Neon API: المشروع ← النقطة الطرفية `read_write` ←
   الفرع الأساسي ← الدور `neondb_owner` (أو أول دور غير محمي) ← `reveal_password` ←
   مضيف `-pooler`، مع ترميز كلمة المرور و`sslmode=require`.
2. الرابط المُستخرَج يمرّ بنفس التحقق الحيّ `SELECT 1` قبل أي كتابة على Vercel، وكلمته
   وDSN كاملًا ينضمان إلى مُنقِّح السجل مثل بقية الأسرار (`***`).
3. مفتاح مقصور على مشروع واحد (`project-scoped`) يكفي: رسالة الـ404 من Neon تسمّي
   المشروع المقصود. وإن رأى المفتاح **أكثر من مشروع** يتوقف السكربت ولا يخمّن — حدِّد
   `NEON_PROJECT_ID` حينها.

مفاتيح اختيارية: `NEON_PROJECT_ID` · `NEON_ROLE` (الافتراضي `neondb_owner`) ·
`NEON_DATABASE` (الافتراضي `neondb`) · وللتعطيل: `scripts/vercel_env_sync.sh --no-neon-fetch`.
لا يُكتب شيء إلى GitHub: القيمة تُشتق على المُشغِّل وتُرسل إلى Vercel مباشرة، فيبقى
GitHub مصدر الحقيقة للأسرار الأخرى، والتحقق الحيّ هو الشرط قبل الكتابة.

### إضافة `VERCEL_TOKEN` (مرة واحدة)

1. [vercel.com/account/tokens](https://vercel.com/account/tokens) → Create Token →
   النطاق (Scope) = الفريق المالك للمشروع (`celia-fashion's projects`) → أنشئه.
2. GitHub → المستودع → Settings → Secrets and variables → Actions →
   New repository secret → الاسم **`VERCEL_TOKEN`** حرفيًا → الصق الرمز.
3. Actions → **Sync production keys to Vercel** → Run workflow.

> بديل بلا رمز: الصق القيم بنفسك في Vercel → Project → Settings → Environment
> Variables (Production) ثم اعمل Redeploy. الأول أصحّ لأن GitHub يبقى مصدر الحقيقة
> الوحيد، ولأن السكربت يتحقق من كل قيمة قبل أن تصل الإنتاج.

### معرّف الفريق والمشروع: القيم الصحيحة وما كان مكسورًا

`VERCEL_TEAM_ID` يُمرَّر كما هو في كل نداء مقيّد (`teamId=…`)، وVercel لا تقبل هناك إلا
**المعرّف** (`team_…`). أي قيمة أخرى — الـslug الظاهر في اللوحة، أو معرّف مشروع، أو قيمة
قديمة من حساب آخر — تُرفَض بـ`403 Not authorized` في القراءة والكتابة معًا، والرسالة تنسب
الرفض إلى الرمز لا إلى المتغيّر، فيمرّ الخطأ على المراجعة باسم «الرمز لا يعمل».

وقع ذلك فعلًا في 2026-10-07: قيمة من 60 حرفًا بلا بادئة `team_` كانت مخزّنة في
`VERCEL_TEAM_ID`، فكانت `/v9/projects` و`/v6/deployments` تردّان `403` **لرمز نفسه** يردّ
`200` بالمعرّف الصحيح. أي أن النشر كان سيتوقف عند أول نداء مقيّد. لذلك صار السكربت:

* يسأل `/v2/teams` أولًا، ويطابق القيمة المخزّنة مع ما يراه الرمز فعلًا؛
* يميّز «لا قيمة مخزّنة» (بحث مشروع) عن «قيمة مخزّنة خاطئة» (عيب يُسمّى صراحةً) ولا
  يستبدل الثانية بالأولى بصمت؛
* يحلّ الفريق والمشروع في `--dry-run` أيضًا، لأن «تحقّق كل شيء» ثم `403` أول نداء مقيّد
  هو الفشل الذي وُجد هذا الترتيب لمنعه.

القيم الصحيحة لهذا المستودع:

| المتغيّر | القيمة | من أين |
|---|---|---|
| `VERCEL_TEAM_ID` | `team_KaDefEE8HBoecXfntoJUw2TZ` | Vercel → Team Settings → Team ID |
| `VERCEL_PROJECT_ID` | `prj_2qy6p3q63at2AZerXZHIxUX3GpJf` | مشروع `cela` (Vercel → Project → Settings → Project ID) |

و`deploy_doctor.py` يفحص الشكل الآن قبل النشر: قيمة `VERCEL_TEAM_ID` لا تبدأ بـ`team_`
خطأ لا تحذير، واسم مشروع بدل `prj_…` تحذير لا خطأ (الاسم يعمل ما دام فريدًا في النطاق).

للتحقق قبل أي إعادة نشر — قراءتان فقط، بلا تغيير في الحالة:

```bash
GITHUB_TOKEN=… GITHUB_REPO=sayedelazameydesign-crypto/1pro \
VERCEL_TOKEN=… VERCEL_PROJECT_ID=prj_2qy6p3q63at2AZerXZHIxUX3GpJf \
VERCEL_TEAM_ID=team_KaDefEE8HBoecXfntoJUw2TZ \
  python scripts/integrations_live_check.py
```

المتوقّع `2 live check(s) passed.` — و`python scripts/integrations_live_check.py
--self-test` يثبت المدقّق نفسه بلا شبكة وبلا أسرار. مسار بديل بلا شبكة محلية: Actions →
**Sync production keys to Vercel** → `dry_run: true`، فهو يفحص القيم ويحلّ الفريق والمشروع
ويسمّي كل مفتاح ناقص دون أن يلمس Vercel.

وإن ظهر `BLOCKED` مع `pre_http_network_failure` (رمز خروج `3`) فذلك فشل قبل وصول أي
استجابة، ولا يقول شيئًا عن الرمز نفسه: السبب — منفذ مغلق أو مرشّح شبكة أو مزوّد أغلق
الاتصال — لا تعرفه طبقة المقبس، فلا تدوّر رمزًا بلا دليل. أعد التشغيل من بيئة تصل إلى
`api.vercel.com`. أما `401`/`403` فهما الحكم على الرمز أو نطاقه، و`--allow-mutations`
وحده يلمس الكتابة. ورمز الخروج `2` يعني أنّ لا فحص قد حاول أصلًا (لا بيانات اعتماد).

### ما تحتاجه صفحة `/integrations` على الإنتاج

الصفحة تقرأ **بيئة الخدمة على Vercel**، لا أسرار GitHub، فمعرّفات القسم السابق لا تكفيها.
تُضاف في Vercel → Project → Settings → Environment Variables (Production) ثم Redeploy:

| المتغيّر | لماذا |
|---|---|
| `WAHA_OWNER_TOKEN` | بلا قيمة تُرجع كل مسارات `/api/owner/*` رمز `503 not_configured` ولا يُقبل دخول أبدًا. ولّده بـ`openssl rand -hex 32` (الطبيب يرفض ما دون 32 حرفًا) |
| `GITHUB_TOKEN` + `GITHUB_REPO` | لقراءة تشغيلات Actions؛ و`GITHUB_WORKFLOW_ID` لتفعيل زر `dispatch`. ملاحظة: GitHub لا يقبل سرًّا باسم `GITHUB_TOKEN`، فالمخزّن هناك يحمل اسمًا آخر ويُسمّى `GITHUB_TOKEN` عند الضبط على Vercel |
| `VERCEL_TOKEN` + `VERCEL_PROJECT_ID` + `VERCEL_TEAM_ID` | لقراءة النشرات. الصفحة **لا تحتاج** رمزًا قادرًا على النشر: النشر يمرّ بالـhook وحده، فرّمز بصلاحية قراءة للمشروع يكفي وهو الأصحّ |
| `DEPLOY_HOOK_URL` | الرابط نفسه هو السرّ؛ بدونه يظهر زرّ النشر معطّلًا بدل أن يعمل |

الرمز على Vercel يبقى في الخادم: `/api/owner/*` هي الوحيدة التي تلمس هذه القيم، وكل
رسالة خارجة تمرّ عبر مُنقِّح الأسرار قبل أن تصل إلى المتصفح.

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
