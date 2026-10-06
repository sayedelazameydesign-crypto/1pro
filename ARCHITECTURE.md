# معمارية واحة · من كتالوج إلى Agent Runtime

وثيقة قرار، لا دليل تشغيل. التشغيل في `README.md`.

## 1. الطبقات

```text
GitHub Pages  (docs/)            واجهة عربية RTL ثابتة، تُقرأ منها config.json
      │  HTTPS + JSON (bearer visitor token)        → polling كل 1.6s + SSE اختياري
      ▼
Flask app     (backend/app.py)   هوية زائر HMAC · CSRF · CORS · حدود الساعة · cooldown
      │
      ▼
Agent Runtime (backend/agent/)   plan → act → observe → reflect → report
      │
      ├── providers.py   Provider interface: Gemini direct · PromptQL gateway · Fake
      ├── tools.py       Tool registry: حساب، وقت، بحث مهارة، ذاكرة، artifact، web_fetch
      ├── store.py       SQLite + Postgres: tasks · steps · events · tool_calls · memory · artifacts
      ├── runtime.py     الحلقة والميزانيات والموافقات
      └── service.py     طابور التنفيذ، خيوط العمل، handshake الموافقة
      │
      ▼
Neon Postgres                    خارج القرص المؤقت؛ لا شيء مهم على نظام الملفات
```

القاعدة الحاكمة: **`agent/` لا تعرف Flask ولا Vendor.** تُحقن بكل شيء من `app.py`
(`Store` يستقبل محوّل `connect/run/insert_returning_id`، و`Agent` يستقبل
`provider_factory`)، لذلك تُختبَر الحلقة كاملة بـ`FakeProvider` بلا شبكة،
ويستطيع مزوّد آخر أن يدخل من نفس الباب.

## 2. لماذا Provider Interface وليس `generate_reply` ممدوداً

المسار القديم (`generate_reply` في `app.py`) يبقى مسؤولاً عن المحادثة التعليمية الواحدة حتى لا نكسر سلوكاً مُختبَراً. طبقة الوكيل تمرّ عبر `agent/providers.py`:

| الكائن | الدور |
|---|---|
| `BaseProvider.complete(system, messages, …)` | إجابة واحدة · `usage` · أخطاء بمعرّفات ثابتة |
| `GeminiProvider` | `generativelanguage.googleapis.com` مباشرة بـ`GEMINI_API_KEY`، ويدعم `alt=sse` للبث |
| `GatewayProvider` | نفس النموذج خلف بوابة PromptQL بمفتاح الزائر الشخصي (`wire="gemini"` أو `"openai"` لـNVIDIA) |
| `FakeProvider` | نص/أوامر مكتوبة مسبقاً؛ للاختبارات و`AGENT_FAKE=1` |

**لا تحويل تلقائي بين المزوّدين**: المزوّد يُختار عند إنشاء المهمة ويبقى لكل
الخطوات، و429 يفشل المهمة برسالة واضحة ويكتب cooldown حيث يكتبه المسار القديم.

## 3. ما ترفضه البنية عمداً

| الرفض | السبب |
|---|---|
| تشغيل كود أو shell داخل الخادم | Render free/Serverless بلا sandbox؛ تنفيذ ما يقترحه نموذج = تنفيذ غير محدود باسم الخادم. الناتج يُولَّد كـ**artifact** ويُعاين في `iframe sandbox` |
| كتابة على نظام الملفات | القرص مؤقت؛ كل شيء في الجداول |
| `web_fetch` بلا موافقة وبلا بوابة | أداة شبكة معطّلة افتراضياً (`AGENT_NETWORK_TOOLS=0`)؛ مفعّلةً تحتاج موافقة صريحة لكل مهمة |
| عناوين داخلية/روابط محلية | حارس SSRF: http(s) فقط، 80/443 فقط، رفض private/loopback/link-local/reserved، إعادة تحقق بعد كل تحويل، سقف 200KB |
| قبول تعليمات من محتوى web | نص الصفحة «بيانات» لا «أوامر»؛ مذكور في system prompt وفي الواجهة |
| سرقة مفاتيح أو وضعها في المتصفح | لا مفاتيح في المستودع/JS؛ المفاتيح في لوحة المزوّد فقط |
| `WAHA_TRUST_PROMPTQL=1` على خدمة عامة | الترويسة تُقرأ بلا توقيع؛ `deploy_doctor` يرفضها |
| ذاكرة بلا سقف | `agent_memory` يدور عند `AGENT_MEMORY_MAX_ITEMS`، ولا يُقرأ منه إلا آخر 8 عناصر كسياق |
| وعد طاقة | `/api/agent/config` يُعلن `guaranteed_capacity: false`؛ الخطة المجانية مشتركة |

## 4. الحلقة والميزانيات

لكل مهمة: `AGENT_MAX_STEPS` خطوة، و`AGENT_MAX_TOOL_CALLS` أداة، و
`AGENT_MAX_AI_CALLS` استدعاء نموذج، و`AGENT_TASK_DEADLINE_SECONDS` زمن — وكل
استدعاء يُخصم من نفس `attempts` التي يخصم منها مسار المحادثة (30/ساعة لكل
مستخدم، 120/ساعة لكل IP)، فلا يستهلك الوكيل الحصة أسرع من إنسان يكتب.

الحالة تُكتب قبل الحدث وبعده (`agent_events`)، والموافقة تنتظر في
`awaiting_approval` حتى `AGENT_APPROVAL_TIMEOUT_SECONDS` ثم **تنتهي ولا تُنفَّذ**.
`Service._work` لا يبتلع الأخطاء: أي استثناء خارج الحلقة يُسجَّل ويحوَّل إلى
`worker_error` بدل أن تبقى المهمة «running» إلى الأبد، و`recover_interrupted()`
في الإقلاع يُصلح ما قطعته إعادة النشر أو نوم instance.

## 5. النشر على المجاني

`render.yaml` يشغّل `gunicorn --workers 1 --threads 8 --timeout 90`:
الخيوط مطلوبة لأن صفحة الوكيل تفتح SSE، وعامل sync واحد بلا خيوط يحبس بقية
الطلبات. `vercel.json` يبقى بديلاً بـ`maxDuration: 60`؛ لذلك تُنفَّذ المهمة داخل الطلب
حين يكون الوضع PromptQL فقط، وفي الوضع المستقل تُدفع إلى الطابور.
