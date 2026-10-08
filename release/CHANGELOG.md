# سجل التغييرات (CHANGELOG)

جميع التغييرات الملحوظة في مشروع **Waha (1pro)** موثقة في هذا الملف وفقًا لمبادئ [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) وتتبع قواعد [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [1.0.0] - 2026-10-08

### مضاف (Added)
- **طبقة إثبات الحدود الحاسوبية (Boundary Proof Recorder)**:
  - سكربت `scripts/record_boundary_proof.py` الذي يرفض كتابة دليل تنفيذي غير قابل للتحقق، ويسجل نتائج تنفيذ 46 اختبار عزل في `tests/test_agent_sandbox.py`.
  - وثيقة `BOUNDARY-PROOF.md` المولدة آليًا لإثبات عزل نظام الملفات (Private Root Chroot)، عزل المعرفات (PID/UID/GID Namespaces)، وفشل المحاولات الهجومية لاختراق المضيف عند حذف الـ chroot.
- **تحديث بيئة التشغيل المستمرة (CI Modernization)**:
  - ترقية جميع إجراءات GitHub Actions في `.github/workflows/` إلى الإصدارات المتوافقة مع Node 24 runtime (`actions/checkout@v7`, `actions/setup-python@v7`, `actions/configure-pages@v6`, `actions/upload-pages-artifact@v5`, `actions/deploy-pages@v5`).
  - إزالة كافة تحذيرات تقادم بيئة التشغيل (Node 20 deprecation warnings).
- **حزمة توثيق الإصدارات**:
  - إنشاء دليل `release/` متضمنًا `CHANGELOG.md` و `RELEASE-NOTES.md`.

### مصحح (Fixed)
- **تصحيح إحصائيات اختبارات العزل والتنفيذ في الوثائق**:
  - تصحيح إجمالي اختبارات `test_agent_sandbox.py` من 41 إلى 46 اختبارًا مطابقًا للواقع الفعلي المقاس ومصفوفة القدرات `CAPABILITY-MATRIX.md`.
  - تصحيح عدد الاختبارات التي يتم تجاوزها (skipped) في بيئات CI المشتركة من 22 إلى 25 اختبار إنفاذ حقيقي، مع تبيان سبب التخطي بأمانة (`unshare: write failed /proc/self/uid_map: Operation not permitted`).

### وثائق (Documentation)
- مواءمة كاملة ومحققة باختبارات الكود (`test_docs_match_code.py` و `test_capability_matrix.py`) لجميع المستندات الهندسية:
  - `README.md`
  - `ARCHITECTURE.md`
  - `CAPABILITY-MATRIX.md`
  - `RAG-FREE-PLAN.md`
  - `BOUNDARY-PROOF.md`

---

## [1.0.0-rc1] - 2026-10-08

### مضاف (Added)
- **المرشح للإصدار الأول (Release Candidate 1)**:
  - تثبيت خط الأساس لجميع الاختبارات (452 Passed, 3 Expected Skips, 0 Failures).
  - تثبيت عقود المتصفح والواجهة (15 بحث، 12 إجابة، 15 مكونات، 18 تكاملات).
  - توثيق الفجوات المعروفة صراحة: بقاء C25 (حدود استهلاك الموارد الإجمالية عبر cgroup) بحالة جزئية (Partial) بالتصميم.

---

## [0.1.0] - 2026-10-05

### مضاف (Added)
- الإطلاق الأولي للنظام المستقل Waha (خادم Flask، دعم Neon Postgres و SQLite، استرجاع RAG المحلي، وتكاملات Gemini / NVIDIA).
