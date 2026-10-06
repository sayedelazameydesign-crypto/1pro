#!/usr/bin/env python3
"""Pre-flight check for the free public deploy (Render or Vercel + Neon + Gemini).

Reads only the environment and the repository: no network calls, no key
printing. It exists because the deployment failure modes we actually hit are
naming and shape mistakes -- `Gemini API Key` with a space instead of
`GEMINI_API_KEY`, a Neon *direct* endpoint behind a serverless runtime, an
origin with a `/1pro` path that the browser will never match, or
`WAHA_TRUST_PROMPTQL=1` left on in a public service.

    python scripts/deploy_doctor.py                     # check the current shell
    python scripts/deploy_doctor.py --env-file .env     # check a local file
    python scripts/deploy_doctor.py --target vercel     # platform-specific rules
    python scripts/deploy_doctor.py --json              # machine readable
    python scripts/deploy_doctor.py --self-test        # used by CI

Exit code: 0 = clear to deploy, 1 = errors (or warnings with --strict).
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
PAGES_ORIGIN = "https://sayedelazameydesign-crypto.github.io"
# NB: no empty string here — "" is a substring of everything and would flag
# every real key. Missing values are handled by the length check above.
PLACEHOLDERS = ("changeme", "change_me", "todo", "xxxx", "none", "your-", "<", "${",
                "your_key_here", "placeholder", "example")
ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def load_env_file(path):
    """Parse KEY=value lines only. Never evaluated, never executed."""
    values = {}
    for raw_line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = ENV_LINE.match(line)
        if not match:
            continue
        key, value = match.group(1), match.group(2)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def _looks_placeholder(value):
    stripped = (value or "").strip()
    lowered = stripped.lower()
    if not stripped:
        return True
    if len(stripped) < 20:
        return True
    return any(token in lowered for token in PLACEHOLDERS)


def check(env, target="render"):
    """-> (errors, warnings, infos) as lists of messages."""
    errors, warnings, notes = [], [], []
    env = {key: (value if value is not None else "") for key, value in env.items()}

    # 1) Names that people type wrong.
    for bad, good in (("Gemini API Key", "GEMINI_API_KEY"), ("GEMINI KEY", "GEMINI_API_KEY"),
                      ("Database URL", "DATABASE_URL"), ("Waha Secret", "WAHA_SECRET")):
        if bad in env:
            errors.append(f"متغير بيئة باسم `{bad}` (فيه مسافات). الاسم الصالح هو `{good}`.")

    # 2) Standalone must not trust the gateway header.
    if env.get("WAHA_TRUST_PROMPTQL", "") == "1":
        errors.append("WAHA_TRUST_PROMPTQL=1 على خدمة عامة: تُقرأ ترويسة الهوية بلا تحقق توقيع، "
                      "فأي زائر ينتحل أي مستخدم. احذفه في Render/Vercel.")
    if env.get("PROMPTQL_PLATFORM_API_URL", "").strip():
        errors.append("PROMPTQL_PLATFORM_API_URL مضبوط: يحوّل مسار AI إلى البوابة ويُلغي "
                      "GEMINI_API_KEY. في النشر المستقل اتركه فارغاً.")

    # 3) The database.
    database_url = env.get("DATABASE_URL", "").strip()
    if not database_url:
        errors.append("DATABASE_URL غير مضبوط. قرص Render وVercel مؤقتان، فلا تصلح SQLite "
                      "لخدمة عامة (ستضيع المحادثات مع كل نشر أو إعادة تدوير).")
    else:
        parts = urlsplit(database_url)
        if parts.scheme.startswith("sqlite"):
            errors.append("DATABASE_URL يشير إلى SQLite. استخدم رابط Neon (postgresql://…).")
        elif "postgres" not in parts.scheme:
            errors.append(f"مخطط DATABASE_URL غير متوقع: {parts.scheme or '(فارغ)'}. "
                          "المطلوب postgresql:// أو postgres://.")
        if "sslmode=require" not in database_url:
            errors.append("DATABASE_URL بلا `sslmode=require`؛ Neon سيرفض الاتصال غير المشفّر.")
        if "channel_binding=require" in database_url:
            warnings.append("`channel_binding=require` موجود: قد لا يدعمه المسار عبر pooler؛ "
                            "احذفه إن فشل الاتصال.")
        host = parts.hostname or ""
        if host and "-pooler" not in host and ".pooler" not in host and "pooler" not in host:
            warnings.append("العنوان يبدو مباشرلاً لا pooled. على Vercel/Serverless يفتح كل استدعاء "
                            "بارد اتصالاً جديداً وقد يستنفد اتصالات Neon؛ استخدم رابط pooled.")
        if target == "vercel":
            warnings.append("Vercel: راقب اتصالات Neon (cold starts) واستخدم رابط pooled دائماً.")

    # 4) Secrets shape and leakage.
    if target == "vercel" and not env.get("WAHA_SECRET", "").strip():
        errors.append("WAHA_SECRET غير مضبوط على Vercel: /tmp زائل، فتُبطل جلسات الزوّار مع كل "
                      "إعادة تدوير. اضبطه في المتغيرات.")
    if not _looks_placeholder(env.get("WAHA_SECRET", "")) and len(env.get("WAHA_SECRET", "")) < 32:
        warnings.append("WAHA_SECRET أقصر من 32 حرفاً؛ ولّد قيمة أطول (secrets.token_hex(32)).")
    if target == "render" and env.get("WAHA_SECRET", "").strip():
        notes.append("WAHA_SECRET مضبوط يدوياً رغم أن render.yaml يولّده (generateValue)؛ "
                     "اليدوي يتجاوز المولَّد — لا بأس به لكن كن متعمَّداً.")

    gemini = env.get("GEMINI_API_KEY", "").strip()
    if not gemini:
        errors.append("GEMINI_API_KEY غير مضبوط: سيعمل الكتالوج والمحادثة محفوظة لكن الردود "
                      "ترجع 503 ai_disabled (مساحة العمل معطّلة بلا نموذج).")
    elif _looks_placeholder(gemini):
        errors.append("GEMINI_API_KEY يبدو نصاً وهمياً/قصيراً؛ ضع مفتاحاً حقيقياً من Google AI Studio.")
    if gemini and gemini.startswith("AIza") and len(gemini) < 30:
        warnings.append("مفتاح Gemini قصير على غير العادة؛ تحقق من نسخه كاملة.")

    # 5) CORS / origins.
    allowed = [item.strip() for item in env.get("WAHA_ALLOWED_ORIGINS", "").split(",") if item.strip()]
    if not allowed:
        errors.append("WAHA_ALLOWED_ORIGINS غير مضبوط: طلبات المتصفح من Pages ستُرفض "
                      "(origin_rejected).")
    for origin in allowed:
        parts = urlsplit(origin)
        if parts.path and parts.path != "/":
            errors.append(f"`{origin}` فيه مسار. المتصفح يقارن الأصل فقط، فاستعمل "
                          f"`{parts.scheme}://{parts.netloc}` (بلا /1pro).")
        if parts.scheme != "https":
            warnings.append(f"أصل غير مشفّر في WAHA_ALLOWED_ORIGINS: {origin}")
    if allowed and PAGES_ORIGIN not in [item.rstrip("/") for item in allowed]:
        errors.append(f" Pages origin `{PAGES_ORIGIN}` غير موجود في WAHA_ALLOWED_ORIGINS.")

    # 6) The static front door's pointer to the API.
    config_path = ROOT / "docs" / "data" / "config.json"
    api_base = ""
    if config_path.exists():
        try:
            api_base = str(json.loads(config_path.read_text(encoding="utf-8")).get("api_base", "")).strip()
        except ValueError as error:
            errors.append(f"docs/data/config.json غير صالح: {error}")
    else:
        errors.append("docs/data/config.json مفقود.")
    if not api_base:
        notes.append("api_base فارغ: Pages سيبقى كتالوجاً ثابتاً (بحث وتنزيل فقط، بلا محادثة أو AI). "
                     "بعد نجاح النشر ضع عنوان الخادم ثم أعد نشر Pages.")
    else:
        parts = urlsplit(api_base)
        if parts.scheme != "https":
            errors.append(f"api_base يجب أن يكون https (الصفحة تُخدم من https): {api_base}")
        if parts.path and parts.path != "/":
            errors.append("api_base يحمل مساراً؛ اترك الأصل فقط (مثال: https://app.onrender.com).")
        if "onrender.com" not in parts.netloc and "vercel.app" not in parts.netloc:
            warnings.append(f"api_base غير مألوف: {parts.netloc} — تحقق أنه عنوان خدمتك.")

    # 7) Repo hygiene: nothing secret should be committed.
    tracked = _tracked_files()
    if tracked is not None:
        leaks = [name for name in tracked
                 if Path(name).name in {".env", "service.env"} or name.endswith((".secret", ".pem", ".key"))
                 or Path(name).name.startswith("credentials") or Path(name).name.startswith("token")]
        if leaks:
            errors.append("ملفات سرّية متتبَّعة في git: " + ", ".join(sorted(leaks)) +
                          " — أزلها من المتتبعة وrotate أي مفتاح انكشف.")
        if not (ROOT / "LICENSE").exists() and not any(name.upper().startswith("LICENSE") for name in tracked):
            warnings.append("لا ملف LICENSE: الكود عام بلا ترخيص يعني «كل الحقوق محفوظة» ولا يجوز "
                            "للآخرين إعادة استخدامه. اختر ترخيصاً قبل التوسّع العام.")
        if not any(name.startswith("backend/") for name in tracked):
            errors.append("مجلد backend/ غير موجود في الشجرة المنشورة؛ النشر لا يعمل من جذر مختلف.")

    # 8) Platform specifics.
    if target == "render":
        blueprint = ROOT / "render.yaml"
        text = blueprint.read_text(encoding="utf-8") if blueprint.exists() else ""
        if not text:
            errors.append("render.yaml مفقود؛ أنشئ الخدمة من Blueprint ليشمل الصحة والمهلات.")
        else:
            if "healthCheckPath: /health" not in text:
                warnings.append("render.yaml بلا healthCheckPath: /health — فاصل الحياة العميق يوقظ Neon.")
            timeout = re.search(r"--timeout (\d+)", text)
            if not timeout or int(timeout.group(1)) < 60:
                errors.append("أمر البدء في render.yaml: --timeout أقل من 60 أو مفقود؛ طلب النموذج "
                              "الواحد قد يستغرق حتى 75 ثانية.")
            if "--threads" not in text:
                warnings.append("أضف --threads إلى gunicorn في render.yaml: صفحة الوكيل تفتح بث أحداث "
                                "(SSE)، وعامل worker واحد بلا خيوط سيحبس الطلبات خلفه.")
            if "plan: free" not in text:
                notes.append("الخطة ليست free في render.yaml — راجع التكلفة.")
        notes.append("Render free: 750 ساعة/شهر لكل workspace، نوم بعد 15 دقيقة خمول، 512MB و0.1 CPU، "
                     "بلا بطاقة ائتمانية — لا تناسب حمل إنتاجي؛ صفّها كـMVP عام.")
    if target == "vercel":
        vercel = ROOT / "vercel.json"
        if not vercel.exists():
            errors.append("vercel.json مفقود.")
        else:
            text = vercel.read_text(encoding="utf-8")
            try:
                data = json.loads(text)
            except ValueError as error:
                errors.append(f"vercel.json غير صالح: {error}")
            else:
                if "builds" in data and "functions" in data:
                    errors.append("vercel.json يجمع builds مع functions: يفشل البناء بـ"
                                  "'Conflicting functions and builds'.")
                duration = (data.get("functions", {}).get("api/index.py", {}) or {}).get("maxDuration")
                if duration != 60:
                    errors.append(f"maxDuration={duration}؛ على Hobby استخدم 60 (300 تُرفض إلا مع "
                                  "Fluid Compute).")
                if not data.get("rewrites"):
                    warnings.append("لا rewrites في vercel.json؛ المسارات الفرعية لن تصل إلى Flask.")
                # R2 reads the committed index at request time; excluding data/ turns
                # /api/search into a 503 on Vercel while the repo's own tests stay green.
                raw = ((data.get("functions") or {}).get("api/index.py") or {}).get("excludeFiles") or []
                if isinstance(raw, str):        # Vercel also accepts "{a/**,b/**}"
                    raw = raw.strip("{}")
                excluded = {item.strip() for item in raw.split(",") if item.strip()} \
                    if isinstance(raw, str) else {str(item) for item in raw}
                if "data/**" in excluded or "data" in excluded:
                    errors.append("vercel.json يستبعد data/**: /api/search لن يجد data/rag/index.json "
                                  "ويرجع 503 على Vercel رغم نجاح الاختبارات في المستودع.")
                if "backend/**" in excluded or "backend" in excluded:
                    errors.append("vercel.json يستبعد backend/**: التطبيق وتوكن RAG لن يوجد أيهما.")
        notes.append("Vercel Hobby للاستخدام الشخصي غير التجاري فقط، و/tmp وحده قابل للكتابة.")

    # 9) The agent knobs fail loudly at import; surface that here too.
    agent_error = _check_agent_config(env)
    if agent_error:
        errors.append(agent_error)
    if env.get("AGENT_NETWORK_TOOLS", "").strip() in ("1", "true", "yes"):
        notes.append("AGENT_NETWORK_TOOLS=1: أداة web_fetch متاحة. تظل محروسة ضد العناوين الداخلية "
                     "وتحتاج موافقة صريحة لكل مهمة (إلا مع AGENT_AUTO_APPROVE_READ_ONLY).")
    if env.get("AGENT_FAKE", "").strip() == "1":
        warnings.append("AGENT_FAKE=1 على خادم عام: الردود نص ثابت من وضع العرض. احذفه بعد التجربة.")

    return errors, warnings, notes


def _tracked_files():
    try:
        import subprocess
        out = subprocess.run(["git", "-C", str(ROOT), "ls-files"], capture_output=True, text=True, timeout=20)
    except (OSError, ValueError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.split()


def _check_agent_config(env):
    """Reuse the app's own validator: AgentConfig raises on out-of-range knobs."""
    saved = {key: os.environ.get(key) for key in list(env)}
    try:
        for key, value in env.items():
            if key.startswith("AGENT_"):
                os.environ[key] = value or ""
        for path in list(sys.modules):
            if path.startswith("agent.config"):
                del sys.modules[path]
        backend = ROOT / "backend"
        if str(backend) not in sys.path:
            sys.path.insert(0, str(backend))
        try:
            from agent.config import AgentConfig  # noqa: F401
        except RuntimeError as error:
            return f"إعدادات الوكيل مرفوضة: {error}"
        except Exception as error:  # noqa: BLE001 - doctor must not crash on an import miss
            return None
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return None


def self_test():
    """Tiny in-process fixture suite so CI proves the doctor itself."""
    good = {
        "DATABASE_URL": "postgresql://user:pass@ep-cool-1234-pooler.us-east-2.aws.neon.tech/db"
                        "?sslmode=require",
        "GEMINI_API_KEY": "A" * 39,
        "WAHA_SECRET": "B" * 64,
        "WAHA_ALLOWED_ORIGINS": PAGES_ORIGIN,
    }
    cases = [
        ("clean env has no errors", dict(good, WAHA_TRUST_PROMPTQL="0"), [], True),
        ("spaces in name", dict(good, **{"Gemini API Key": "x"}), None, False),
        ("trust promptql refused", dict(good, WAHA_TRUST_PROMPTQL="1"), None, False),
        ("missing db", {k: v for k, v in good.items() if k != "DATABASE_URL"}, None, False),
        ("origin with path", dict(good, WAHA_ALLOWED_ORIGINS=PAGES_ORIGIN + "/1pro"), None, False),
        ("sqlite db", dict(good, DATABASE_URL="sqlite:///x.db?sslmode=require"), None, False),
        ("placeholder key", dict(good, GEMINI_API_KEY="changeme"), None, False),
    ]
    failures = []
    for label, env, _expected, should_pass in cases:
        errors, _warnings, _notes = check(env, target="render")
        ok = (not errors) if should_pass else bool(errors)
        if not ok:
            failures.append(label)
    # Direct (non-pooled) endpoint is a warning, not an error.
    direct = dict(good, DATABASE_URL=good["DATABASE_URL"].replace("-pooler", ""))
    errors, warnings, _notes = check(direct, target="render")
    if errors or not any("pooled" in item for item in warnings):
        failures.append("direct endpoint should warn, not fail")
    # A vercel deploy without WAHA_SECRET must fail.
    errors, _w, _n = check({k: v for k, v in good.items() if k != "WAHA_SECRET"}, target="vercel")
    if not any("WAHA_SECRET" in item for item in errors):
        failures.append("vercel needs WAHA_SECRET")
    return failures


def main(argv=None):
    parser = argparse.ArgumentParser(description="Pre-flight checks for the free public deploy.")
    parser.add_argument("--env-file", help="read KEY=value pairs from this file instead of the shell")
    parser.add_argument("--target", choices=("render", "vercel", "both"), default="render")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--strict", action="store_true", help="treat warnings as failures")
    parser.add_argument("--self-test", action="store_true", help="run the doctor's own fixtures")
    args = parser.parse_args(argv)

    if args.self_test:
        failures = self_test()
        if failures:
            print("deploy_doctor self-test FAILED: " + "; ".join(failures))
            return 1
        print("deploy_doctor self-test OK")
        return 0

    if args.env_file:
        path = Path(args.env_file)
        if not path.exists():
            print(f"--env-file not found: {path}")
            return 2
        env = load_env_file(path)
    else:
        env = dict(os.environ)

    targets = ("render", "vercel") if args.target == "both" else (args.target,)
    all_errors, all_warnings, all_notes = [], [], []
    for target in targets:
        errors, warnings, notes = check(env, target=target)
        all_errors += [f"[{target}] {item}" for item in errors]
        all_warnings += [f"[{target}] {item}" for item in warnings]
        all_notes += [f"[{target}] {item}" for item in notes]

    if args.json:
        print(json.dumps({"ok": not all_errors and not (args.strict and all_warnings),
                          "errors": all_errors, "warnings": all_warnings, "notes": all_notes},
                         ensure_ascii=False, indent=2))
    else:
        def show(kind, items):
            for item in items:
                print(f"{kind}  {item}")
        if all_errors:
            print("== ما يمنع النشر ==")
            show("ERROR", all_errors)
        if all_warnings:
            print("== يستحق مراجعة ==")
            show("WARN ", all_warnings)
        if all_notes:
            print("== ملاحظات ==")
            show("NOTE ", all_notes)
        if not all_errors and not all_warnings and not all_notes:
            print("OK: كل شيء جاهز للنشر.")
        elif not all_errors:
            print("\nلا أخطاء: يمكن المتابعة. (الأوامر الجاهزة في README → «الخطوات المختصرة»)")

    if all_errors or (args.strict and all_warnings):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
