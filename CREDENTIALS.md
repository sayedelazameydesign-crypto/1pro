# أقل صلاحية ممكنة · لكل credential في هذا المشروع

وثيقة قرار، مش checklist نشر. كل سطر هنا يقول: ما أدنى مجموعة صلاحيات تُبقي
الوظيفة شغّالة، ولماذا السطو على هذا السرّ بالذات لا يجب أن يمنح أكثر من ذلك القدر.
القاعدة التي تُشتَقّ منها كل الصفوف:

> **مقياس الخطر هو ما يستطيع الحامل فعله، لا ما يمثّله الحامل.**
> لذلك كل تكامل هنا مفصول إلى نصفيْن: رمز قراءة لا ينشر، ورابط نشر لا يقرأ.
> `backend/integrations/config.py` يقرأ الاثنين من مكانين مختلفين، ولا يوجد متغيّر
> واحد يجمعهما — وهذا مقصود لدرجة أن له اختبارًا.

## الجدول

| الـcredential | أقل صلاحية تكفي | أين تعيش | ماذا يحدث لو تسرّبت | كيف تتأكد |
|---|---|---|---|---|
| `GITHUB_TOKEN` (تقرأه البوابة) | **fine-grained PAT**: Resource owner = حسابك، `Only select repositories` → `1pro`، و**`Actions: read + write`** فقط. `metadata: read` تلقائي. لا `contents`، لا `workflows: write`، لا `repo` | Render → Environment (سرّ، مش variable) | المهاجم يشغّل workflow على `main` — وهو أسوأ ما هنا، لأنه تنفيذ كود بأسرار الـrunner؛ لكن لا قراءة كود ولا push | `python scripts/integrations_live_check.py` → صف `GitHub / GET workflow runs` لازم `PASS` |
| `VERCEL_TOKEN` | **Read Only** (أو أضيق scope متاح في لوحتك، ومقيَّد بالـproject/الفريق). لا يحتاج write إطلاقًا: النشر مش شغلته | GitHub Secrets → ينقله `Sync production keys to Vercel` workflow إلى Vercel Production | قراءة اسماء المشاريع/النشرات والمتغيّرات العامة؛ **لا** نشر ولا تعديل env | نفس الأمر → `Vercel / GET deployments` |
| `DEPLOY_HOOK_URL` (Vercel) | **لا شيء يُختار**: الرابط هو الصلاحية، وهو كتابة فقط | Vercel → Project → Settings → Deploy Hooks | نشر واحد لكل طلب على الرابط؛ لا قراءة ولا حذف. mitigations: لا يُخزَّن في git، ولا يُطبع، ولا يُمرَّر لـ`live-check.yml` | `live-check` يعرضه `SKIP · needs --allow-mutations` لا `PASS` |
| `RENDER_API_KEY` | Render **معندوش keys محدودة النطاق**: مفتاح الحساب = كل الحساب. هذا فرق بنيوي عن Vercel، ونتيجته أن فصل القراءة/الكتابة هنا أهم لا أقل | Render → Environment | قراءة وكتابة على كل خدمات الحساب — أوسع انفجار في الجدول. لو في بديل App-level use في لوحتك، فضّله | `Render / GET deploys` = `PASS` |
| `RENDER_DEPLOY_HOOK_URL` | رابط الـhook فقط؛ لا API key معه | Render → Service → Settings → Deploy Hooks | نشر على خدمة واحدة، ولا قراءة. **فرق عن Vercel**: السرّ في `?key=` داخل الـquery، والـquery يُسجَّل في البروكسيات أسهل من الـpath — فالرابط كامل داخل `config.secrets()` وممنوع أن يظهر في أي ردّ أو خطأ | اختباران في `tests/test_integrations_portal.py` يمنعا ظهور `?key=` في الردّ وفي رسالة الخطأ |
| `GOOGLE_DRIVE_ACCESS_TOKEN` | تجربة فقط. عمره ~60 دقيقة، ولا يُجدَّد، ولا يجب أن ينام في environment دائم | Render → Environment (مؤقت) أو shell عند التشغيل | الوصول لحساب Drive حسب الـscopes الممنوحة *للاست consenting* — انظر صف الـscopes | `describe()["drive"]["credential"] == "access_token"` |
| `GOOGLE_DRIVE_REFRESH_TOKEN` + `GOOGLE_DRIVE_CLIENT_ID` + `GOOGLE_DRIVE_CLIENT_SECRET` | الخيار الدائم. **الـscopes هي الضابط، لا نوع الرمز**: `https://www.googleapis.com/auth/drive.readonly` (للتعداد) + `https://www.googleapis.com/auth/drive.file` (للإنشاء). **لا** `drive` الكامل | Render → Environment | `drive.file` وحده لا يكفي للتعداد: يرى الملفات التي أنشأها التطبيق فقط، فمجلد موجود سابقًا يبان فاضي — وهذا «نجاح» كداب. و`drive` الكامل يعني حذف/مشاركة أي ملف في الحساب | `drive / GET folder` = `PASS` بعدد ملفات يطابق ما تراه في المتصفح |
| `GOOGLE_DRIVE_FOLDER_ID` | **ليس سرًّا** — locator. لا تضع فيه أي قيمة تحمل `'/"/?#` (تُقفل `q='…' in parents`) | Render → Environment، أو `vars` على GitHub | كشف موقع مجلد واحد. لا أكثر، ولا أقل — ولهذا يُطبع في الواجهة كأسماء المتغيّرات الناقصة لا كقيمة | `python scripts/deploy_doctor.py --target=render` يرفض الصيغة المكسورة |
| `WAHA_OWNER_TOKEN` | عشوائي ≥32 حرفًا؛ `openssl rand -hex 32` هو الصيغة المقصودة. **مفيش scope**: هو البوابة كلها | Render → Environment | فتح `/integrations`؛ أي كتابة فيه تحتاج نصّ تأكيد + حدّ 3/دقيقة + CSRF + قائمة CORS للإدارة | `login` بلا رمز صحيح = `401`؛ والصفحة بلا `WAHA_OWNER_TOKEN` = `503 not_configured` |
| `DATABASE_URL` (Neon) | **pooled** للـruntime (PgBouncer، اتصال قليل العدد على instance مجاني). الـ**direct/private** URL لمشغّل الـmigrations فقط، ولا يوضع في Vercel | Render/Vercel → Environment | سرقة بيانات + DDL. لا شيء في الكود يستدعي الـdirect URL في الطلبات | `scripts/deploy_doctor.py` يعمل `SELECT 1` حيًّا على الـpooled فقط |
| `GEMINI_API_KEY` | مفتاح محدود **بالـAPI** (Generative Language API) فقط؛ تقييد الـreferrer/IP غير مطبَّق لأن النداء من خادم | Render → Environment | استهلاك حصة فواتير على API واحد | مفحوص في `deploy_doctor`؛ وحجم الاستخدام في `scripts/rag_eval.py` |
| `WAHA_SECRET` | قيمة عالية الإنتروبيا، تُوقّع رموز الزوار ورمز جلسة المالك (prefix مختلف لكل واحد) | Render → Environment | **تزييف جلسة زائر أو مالك**. لهذا الفصل بينهما مهم: `verify_owner_token` بيرفض رمز الزائر حتى لو نفس `WAHA_SECRET` | `test_a_visitor_token_cannot_open_the_owner_surface` |

## قواعد مُشتَقّة من الجدول، لا إضافات إليه

1. **لا سرّ في git، ولا سرّ في `vars` على مستودع عام.** أسماء المتغيّرات عامة، وقيمها
   أسرار: `sync-vercel-env.yml` بينبّه بصراحة لما يلاقي قيمة مكانة في `vars` لأن
   ريبتنا public — والنصيحة نفسها تنطبق على `RENDER_*`/`GOOGLE_DRIVE_*`.
2. **لا تطلب صلاحية يمكن الاستغناء عنها بزر واحد.** صلاحيات الـGitHub App الخاص
   بـArena هنا `contents/workflows: write` (تكفي النشر وفتح PR) لكن
   `actions: write` مفقودة، فـ`gh workflow run` بيرجع `403 Resource not accessible
   by integration`. الحلّ تعديل صلاحيات الـ**installation** — لا إضافة collaborator
   ولا تغيير أدوار: هذا حساب شخصي (`type: User`)، فالمستويات المتاحة هي owner و
   collaborators فقط، والتوكن ما بيوقّعش باسم المستخدم أصلًا.
3. **لا تبدّل قناة القراءة بشيء يقطعها.** واجهة Checks API مذكورة رسميًّا في قائمة
   *فجوات* fine-grained PATs، وهي القناة التي تُقرأ منها نتائج `live-check.yml` عن
   بعد (`…/check-runs/<id>/annotations`). فلو بَدلت اتصال الـAgent بمفتاح fine-grained
   هتخسر التفسير الآلي للنتايج — والأفضل يبقى App-based.
4. **لا تخلط القراءة بالكتابة في نفس الرمز حتى لو كان أسهل.** القاعدة دي هي السبب في
   أن `RENDER_DEPLOY_HOOK_URL` مش داخل `render.yaml` ولا داخل environment بتاع
   `live-check.yml`: job قراءة-only ما يحتاجش وجهة نشر في بيئته، ومنعُه أقوى من
   منعه بالـflag.

## ما هو مُتحقَّق منه هنا، وما هو قرار من وثائق خارجية

| الحكم | حالته |
|---|---|
| أن `live-check.yml` لا يمرّر `--allow-mutations` ولا `RENDER_DEPLOY_HOOK_URL` | **مقروء في الملف**، ومُختبَر: الخطوة بتطبع `SKIP` للكتابات الأربع |
| أن `?key=` ما بيظهرش في ردّ ولا في خطأ | **مُختبَر** في `tests/test_integrations_portal.py` |
| أن المستودع مملوك لحساب `User` | **مقاس**: `gh api users/<owner> → "type": "User"` |
| أن القراءة على مستودع عام مش محتاجة صلاحية | **مقاس**: `GET …/actions/runs` بدون auth = `200`. consequence: نجاح `gh run list` **لا** يثبت إن الـApp عنده `actions: read` — القراءة على public repo متاحة لأي حدّ أو بلا توكن. الشيء الوحيد المُثبَت هو أن **الكتابة** مرفوضة (`403` على `dispatches` وعلى `actions/secrets/public-key`) |
| «`X-OAuth-Scopes` فاضي ⇒ مش PAT» | **مش دليل كافي**، وصحّحته مهم: fine-grained PATs كمان ما ليهاش scopes في الهيدر. وده اللي طلع فعليًّا: الـenv فيه مقبض خاص بالمنصّة (`len=24`، بادئة `arena-eg…`) **مش** توكن GitHub بصيغته المعروفة (`ghp_`/`github_pat_`/`ghs_`)، فالطلبات بتتوسَّط عبر تكامل App — والاستنتاج الصحيح هو أن **نوع الـcredential خارج سيطرة المستخدم ومنظومته**، لا أن «مفيش scopes معناه App» |
| فجوة Checks API في fine-grained، وأن `Actions: read/write` متاحة للمستودعات المملوكة لمستخدم | مأخوذة من وثائق GitHub (صفحة *Managing your personal access tokens*). **مش متحقق منها من جوه الصندوق** لأن `docs.github.com` خارج قائمة الشبكة المسموح بها |
| أن Render معندوش scoped keys، وأن `VERCEL_TOKEN` يكفي فيه Read Only | قرار من لوحات المزوّدين؛ ما اتقاسش هنا. لو اللوحة عندك بتدي أنضج من كده، خُد الأنضج وكتّب السطر في هذا الملف |

## إعادة التحقق في أمر واحد

```bash
python scripts/deploy_doctor.py --target=render && python scripts/deploy_doctor.py --target=vercel
python scripts/integrations_live_check.py            # 4 قراءات + 4 كتابات SKIP، ومفيش شبكة مطلوبة عشان SKIP يظهر
python scripts/integrations_live_check.py --self-test # 43 فحصًا: المنطق نفسه، بلا مفاتيح
```
