# واحة على متجر Vercel (تكامل أصلي · Native Integration)

هذا المستند دليل تشغيل **مزوّد** (provider): واحة تعرض منتجًا في متجر Vercel، وVercel
هو الذي ينادينا. العلاقة معكوسة بالنسبة إلى `DEPLOY-VERCEL.md` و`backend/integrations/`
حيث **نحن** ننادي GitHub وVercel خلف جلسة المالك — لذلك الفصل بينهما حزمات لا مجلّدات.

| | `backend/integrations/` | `backend/marketplace/` |
|---|---|---|
| من ينادي | نحن | Vercel |
| من يصادق | جلسة المالك (`WAHA_OWNER_TOKEN`) | توكن OIDC موقّع بمفتاح Vercel + HMAC للويب هوك |
| ماذا يفعل | قراءة تشغيلات CI وإطلاق نشر | إنشاء تثبيت، توفير مورد، حقن متغيرات بيئة |
| الطرق | `/api/owner/…` و`/integrations` | `/v1/…` و`/marketplace/…` |

الحزمتان تتشاركان القواعد (حزمات بلا Flask، نقل محقون، إعدادات جامدة، تمويه للأسرار)
ولا تتشاركان سطرًا واحدًا من الكود.

---

## 1) ما يعمل الآن، وما لا يعمل

**يعمل:** تثبيت OAuth2، تحقّق RS256 من توكنات Vercel عبر JWKS، توفير مورد (مساحة عمل +
رمز واجهة)، حقن ثلاثة متغيرات بيئة في مشروع العميل، تدوير الرمز مع إبطال فعلي للقديم،
استقبال ويب هوك موقّع مع منع التكرار، ولوحة عربية RTL لا تعرض أي سرّ.

**لا يعمل — ومُعلَن هكذا في `CAPABILITY-MATRIX.md` لا مُخفَّأ:**

- **الفوترة.** لا فواتير، ولا خطط مدفوعة، ولا استخدام مُبلَّغ. الخطة الوحيدة مجّانية
  وتقول ذلك (`paymentMethodRequired: false`). السبب: الفوترة عبر Vercel تحتاج قبولًا في
  [برنامج الشركاء](https://vercel.com/marketplace/program) ومسارات `/billing` كاملة،
  وأي ادّعاء نصفه هنا يصبح وعدًا لا يفي به الكود.
- **استيراد موارد قائمة** (`Import Resource URL`) و**فحوص النشر** (Deployment Checks).
- **SSO برمز OIDC للمورد** — العميل يحصل على رمز واجهة طويل الأمد محقون في بيئته، لا على
  رمز قصير الأمد يُستبدل عند كل نداء.

## 2) نصّ التسجيل: ما تُلصقه في نموذج Vercel

النموذج في **Integrations Console ← Create**. الحقول أدناه جاهزة للّصق. سرد المتجر
بالإنجليزية لأن المتجر نفسه سوق عام بالإنجليزية؛ إن أردت قائمة عربية بدّل القسمين
المحصورين أدناه بالبديل العربي في نهاية هذا القسم.

### الحقول

| الحقل | القيمة |
|---|---|
| **App name** | `Waha — Arabic AI Workspace` |
| **URL Slug** | `waha` |
| **Developer** | `Sayed Elazamey` (الاسم القانوني للمالك) |
| **Contact Email** | بريدك غير المنشور |
| **Support Contact Email** | بريد الدعم المنشور |
| **Short Description** | `Arabic-first AI workspace for your Vercel project. One-click provisioning, credentials injected as environment variables.` |
| **Category** | `AI` (أو `Developer Tools`) |
| **Website** | `https://sayedelazameydesign-crypto.github.io/1pro/` |
| **Documentation URL** | رابط هذا الملف في المستودع |
| **EULA URL** | رابط ترخيصك (Apache-2.0 في هذا المستودع، راجع `LICENSE`) |
| **Privacy Policy URL** | رابط سياسة الخصوصية |
| **Redirect URL** | `https://<خدمة واجهة برمجية>/marketplace/configure` |
| **Webhook URL** | `https://<خدمة واجهة برمجية>/v1/webhooks/vercel` |
| **Base URL** | `https://<خدمة واجهة برمجية>` |
| **Redirect Login URL** | `https://<خدمة واجهة برمجية>/marketplace/callback` |
| **Configuration URL** | `https://<خدمة واجهة برمجية>/marketplace/configure` |
| **Installation-level Billing Plans** | **معطّل** (الفوترة على مستوى المورد) |

`<خدمة واجهة برمجية>` هو نشر Flask نفسه — `https://cela-umber.vercel.app` اليوم. يجب أن
تطابق قيمة `WAHA_MARKETPLACE_BASE_URL` حرفيًا، لأنها تُحقن كما هي في مشاريع العملاء.

### API Scopes المطلوبة

| النطاق | المستوى | لماذا |
|---|---|---|
| `integration-configuration` | Read | قراءة التثبيت وصلاحياته |
| `project` | Read | معرفة المشاريع المتصلة بالمورد |
| Project Environment Variables (مملوكة للتكامل) | Read & Write | حقن المتغيرات في المشاريع المتصلة |

لا تطلب أكثر من ذلك: كل نطاق إضافي هو بيانات مستخدم أنت مسؤول عنها أمام Vercel.

### Overview (حدّ ٧٦٨ حرفًا)

```markdown
Waha is an Arabic-first AI workspace: guided explanations, exercises, quizzes and a
general assistant, behind one API. Install it from the Vercel Marketplace and every
project you connect gets its own workspace plus a scoped API token, injected straight
into your environment variables — no dashboard hopping, no copy-paste of secrets.

What you get:

- **One workspace per resource.** Name it, connect it to any number of projects.
- **Three environment variables.** `WAHA_API_BASE`, `WAHA_API_TOKEN` and
  `WAHA_WORKSPACE_ID` appear in Production, Preview and Development.
- **Rotation that actually revokes.** Rotate from the resource page: the new value is
  pushed to Vercel and the old token stops authenticating immediately.
- **An Arabic RTL console** that shows fingerprints, never secrets.

Waha is free today: a single free plan, no card, no invoice. Billing through Vercel is
not enabled yet.
```

### Additional Information (حدّ ١٠٢٤ حرفًا)

````markdown
### Environment variables injected into your project

| Variable | Purpose |
|---|---|
| `WAHA_API_BASE` | The Waha API root your project should call. |
| `WAHA_API_TOKEN` | Bearer token scoped to this workspace. Treat as a secret. |
| `WAHA_WORKSPACE_ID` | Stable identifier for the workspace behind the token. |

All three are set for Production, Preview and Development. They are owned by the
integration, so removing the resource removes them.

### Try the token

```sh
curl -s "$WAHA_API_BASE/api/me" -H "Authorization: Bearer $WAHA_API_TOKEN"
```

A `200` with `authenticated: true` means the connector is wired end to end.

### If a token leaks

Open the resource in Vercel and rotate. Rotation mints a new token, pushes it to every
connected project, and the previous value stops working on the next call — there is no
window in which both are valid.
````

### البديل العربي (إن أردت قائمة عربية)

> **App name:** `واحة · Waha`
> **Short Description:** `مساحة عمل ذكية بالعربية لمشروعك على Vercel: إنشاء بنقرة واحدة،
> والمفاتيح تُحقن تلقائيًا في متغيرات البيئة.`

## 3) مخطط البيانات الوصفية (Metadata Schema)

يُنسخ كما هو في **Integration Console ← المنتج ← Metadata Schema**، وهو نفس الكائن
المُعرَّف في `backend/marketplace/config.py` (`METADATA_SCHEMA`) والذي يُتحقَّق منه في
`MarketplaceService._validate_metadata` — فالنموذج والفحص لا يفترقان:

```json
{
  "type": "object",
  "properties": {
    "workspace_name": {
      "type": "string",
      "title": "اسم مساحة العمل",
      "minLength": 3,
      "maxLength": 40
    },
    "focus": {
      "type": "string",
      "title": "المجال",
      "enum": ["general", "learning", "assistant"],
      "default": "general"
    }
  },
  "required": ["workspace_name"],
  "additionalProperties": false
}
```

## 4) المتغيرات على خادمنا

| المتغير | مطلوب | ما يفعله |
|---|---|---|
| `VERCEL_INTEGRATION_CLIENT_ID` | نعم | Integration ID بصيغة `oac_…`؛ يُتحقَّق منه كـ`aud` |
| `VERCEL_INTEGRATION_CLIENT_SECRET` | نعم | سرّ OAuth؛ يُوقَّع به كل ويب هوك ويُستبدل به رمز التثبيت |
| `WAHA_MARKETPLACE_BASE_URL` | نعم | رابط خدمتنا العام؛ يُحقن كـ`WAHA_API_BASE` |
| `WAHA_MARKETPLACE_REDIRECT_URL` | نعم | يجب أن يطابق **Redirect URL** في الـConsole حرفيًا |
| `VERCEL_INTEGRATION_SLUG` | لا | يُعرض في `/api/owner/marketplace` |
| `WAHA_MARKETPLACE_PRODUCT_ID` | لا | افتراضي `waha-workspace` |
| `WAHA_MARKETPLACE_ENV_PREFIX` | لا | افتراضي `WAHA`؛ يغيّر أسماء المتغيرات المحقونة |
| `WAHA_MARKETPLACE_MAX_RESOURCES` | لا | افتراضي 5 لكل تثبيت |
| `WAHA_MARKETPLACE_SESSION_TTL` | لا | افتراضي ٨ ساعات لجلسة اللوحة |
| `WAHA_MARKETPLACE_STATE_TTL` | لا | افتراضي ١٥ دقيقة |
| `WAHA_MARKETPLACE_TIMEOUT` | لا | افتراضي ١٥ ثانية لنداءات Vercel |

بلا المتغيرات الأربعة الأولى، كل مسارات `/v1/…` تردّ `503 not_configured` — لا تفتح
الباب جزئيًا. مدقّق النشر يفحصها:

```sh
python scripts/deploy_doctor.py --target vercel     # يحذّر/يُخطّئ إن نُصفت أو اختلف المضيف
scripts/vercel_env_sync.sh                          # يدفعها إلى Vercel مع باقي الأسرار
```

## 5) المسارات

التثبيت والتسجيل (المتصفح):

| المسار | من يفتحه | ماذا يفعل |
|---|---|---|
| `GET /marketplace/configure` | Vercel أثناء التثبيت | يبادل `code` برمز وصول، يخزّنه، يوجّه إلى `next` (إن كان على `*.vercel.com`) |
| `GET /marketplace/callback` | زر «Open in Provider» | يبادل `code` بتوكن هوية، يتحقّق منه، يفتح اللوحة |
| `GET /marketplace` | العميل | لوحة عربية؛ بلا جلسة موقّعة تعرض «الدخول مطلوب» فقط |

واجهة الشريك (يناديها Vercel، كلها تحت `Base URL`):

| المسار | الغرض |
|---|---|
| `PUT /v1/installations/{id}` | إنشاء/تحديث التثبيت (يحمل رمز وصول التثبيت) |
| `GET /v1/installations/{id}/plans` · `GET /v1/products/{pid}/plans` | خطط الفوترة |
| `GET·POST /v1/installations/{id}/resources` | قائمة الموارد · توفير مورد جديد |
| `GET·PATCH·DELETE /v1/installations/{id}/resources/{rid}` | قراءة وتعديل وحذف |
| `POST /v1/installations/{id}/resources/{rid}/secrets/rotate` | تدوير الرمز |
| `POST /v1/webhooks/vercel` | أحداث Vercel (توقيع HMAC-SHA1) |

للمالك: `GET /api/owner/marketplace` يعيد الحالة (منطقيات، أعداد، بصمات — بلا أسرار).

## 6) مسار التثبيت، سطر بسطر

```text
العميل: Storage ← Create Store ← اختيار منتج واحة
   │
   ├─ Vercel ──PUT /v1/installations/{icfg_…}──▶ نحن
   │             (يحمل credentials.access_token + بيانات الحساب)
   │                                            نخزّن التثبيت · 201
   │
   ├─ Vercel ──GET /v1/installations/{id}/plans──▶ نحن   (خطة مجّانية واحدة)
   │
   └─ العميل يضغط Create ──POST /v1/installations/{id}/resources──▶ نحن
                                        نولّد مساحة عمل + رمزًا · 201 + secrets
                                        Vercel يحقنها في المشاريع المتصلة
      (أوّل مرة فقط) المتصفح ← Redirect URL ← /marketplace/configure ← تبادل الرمز
```

كل نداء من Vercel يُصادق قبل أن تُقرأ ترويسة واحدة من جسمه، و`installation_id` في
التوكن يُقارَن بـ`installation_id` في المسار، وعمليات الكتابة تشترط دور `ADMIN`.

## 7) الأمان: ما هو مضمون وما هو ليس كذلك

**مضمون:**

- توقيع التوكنات يتحقّق من مفاتيح Vercel المنشورة، لا من أي مفتاح في الطلب. `alg` يجب أن
  يكون `RS256` حرفيًا — `none` و`HS256` وبقية الخوارزميات مرفوضة قبل لمس المفتاح، وهو ما
  يوقف هجوم الخلط بين الخوارزميات.
- مفوّضات الاختبار مفاتيح RSA-2048 حقيقية من OpenSSL، لا محاكاة
  (`tests/test_marketplace_crypto.py`).
- الرمز المُدار يبطل فعليًا: التوكن الموقّع بلا حالة لا يمكن «إلغاء توقيعه»، لذا تُقارَن
  القيمة المقدَّمة بالمخزَّنة في قاعدة البيانات. تدوير بلا إبطال مسرحية.
- اللوحة تعرض بصمات لا أسرار، وبلا جلسة موقّعة لا تعرض شيئًا — صفحة فارغة كانت ستكون
  ثغرة تعداد لمعرّفات التثبيتات.

**غير مضمون، وبصراحة:**

- **التشفير عند التخزين.** رموز وصول التثبيتات وأسرار الموارد تُخزَّن كما هي في
  `DATABASE_URL`. التشفير على القرص مسؤولية المشغّل (Neon وRender يوفّرانه). تشفير
  تطبيقي بتيار مفاتيح homemade سيبدو حلًّا وهو ليس كذلك — والتخفيف الحقيقي موجود: التدوير.
- **التحقّق من RS256 مكتوب هنا لا في مكتبة.** لا نريد `cryptography` في دالة serverless،
  ولذلك `backend/marketplace/crypto.py` يُنفّذ مسارًا واحدًا فقط (RS256 بـPKCS#1 v1.5)
  ولا يولّد مفاتيح ولا يوقّع ولا يفاوض خوارزميات. إن أضفنا `PyJWT[crypto]` يومًا، الاستبدال
  في ملف واحد والاختبارات نفسها تحكم.
- **SQLite على Vercel زائل.** قالب `/tmp`: التثبيتات تختفي مع إعادة التدوير. اضبط
  `DATABASE_URL` قبل اعتباره نشرًا حقيقيًا.

## 8) التجربة محليًا

```sh
python -m venv .venv && .venv/bin/pip install -r backend/requirements.txt
export VERCEL_INTEGRATION_CLIENT_ID=oac_… VERCEL_INTEGRATION_CLIENT_SECRET=…
export WAHA_MARKETPLACE_BASE_URL=http://localhost:5210
export WAHA_MARKETPLACE_REDIRECT_URL=http://localhost:5210/marketplace/configure
.venv/bin/python scripts/marketplace_demo_seed.py     # صفوف تجريبية + خادم على :5210
```

ثم افتح الرابط الذي يطبعه السكربت. **هذه الصفوف وهمية**: لوحة تعرض بيانات لا تعني أن
تثبيتًا حقيقيًا نجح. التثبيت الحقيقي يحتاج HTTPS عامًا (Vercel لا يوجّه إلى `http://`
ولا إلى `localhost`).

## 9) المراجع

- [Create an Integration](https://vercel.com/docs/integrations/create-integration)
- [Integrations REST API](https://vercel.com/docs/integrations/create-integration/marketplace-api)
- [Partner API reference](https://vercel.com/docs/integrations/create-integration/marketplace-api/reference/partner)
- [Native Integration Flows](https://vercel.com/docs/integrations/create-integration/marketplace-flows)
- [Example Marketplace Integration](https://github.com/vercel/example-marketplace-integration)
