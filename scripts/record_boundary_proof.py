#!/usr/bin/env python3
"""Record a code-execution boundary proof -- or refuse to write one.

The shared `ubuntu-latest` runner forbids unprivileged user namespaces, so the
enforcement tests in `tests/test_agent_sandbox.py` SKIP there and `ci.yml` prints an
annotation saying so. That annotation is honest, and it is also a standing gap: the
repository holds no run in which the boundary was actually built and then attacked.

This script produces that missing evidence on a host that can build the boundary --
and refuses to produce it anywhere else. A transcript written on a machine where

    unshare: write failed /proc/self/uid_map: Operation not permitted

was the answer would be the same lie as a green tick over a skipped test, so a skip
is a non-zero exit here, not a footnote.

    python scripts/record_boundary_proof.py --out BOUNDARY-PROOF.md

Every number in the output is read from the run that produced it, including the
contrast figure for the GitHub runner, which is measured by replaying `detect()` with
that runner's own refusal text rather than quoted from memory. The file is generated,
never hand-edited: a proof that can drift from its source is not a proof.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import platform
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from agent import sandbox  # noqa: E402

PATTERN = "test_agent_sandbox.py"
ENFORCEMENT_CLASS = "TheBoundaryHoldsAgainstRealCode"
# The exact text `ci.yml` relays from the shared runner, and the one the replay uses.
GITHUB_REFUSAL = b"unshare: write failed /proc/self/uid_map: Operation not permitted"
EXIT_CANNOT_PROVE = 2
EXIT_DID_NOT_PROVE = 1


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


def run_suite():
    """Run the sandbox file at verbosity 2 and keep both the result and the log."""
    loader = unittest.TestLoader()
    suite = loader.discover(str(ROOT / "tests"), pattern=PATTERN)
    ids = [test.id() for test in flatten(suite)]
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    return {
        "collected": len(ids),
        "ran": result.testsRun - len(result.skipped),
        "skipped": [test.id() for test, _ in result.skipped],
        "failures": [test.id() for test, _ in result.failures],
        "errors": [test.id() for test, _ in result.errors],
        "enforcement": [i for i in ids if f".{ENFORCEMENT_CLASS}." in i],
        "log": stream.getvalue(),
    }


def replay_without_a_boundary():
    """What this file does on a runner whose kernel refuses user namespaces.

    Run in a subprocess on purpose: the module decides `BOUNDARY_AVAILABLE` at import
    time, so the answer has to be produced by a fresh interpreter, not by patching a
    module that already read it. The wrapper keeps `detect()`'s real contract for the
    tests that pass their own probes -- an earlier version of this measurement stubbed
    `detect` outright and produced three failures that were artifacts of the stub,
    which is its own small lesson about measuring a boundary with a fake.
    """
    program = f'''
import io, json, subprocess, sys, unittest
sys.path.insert(0, {str(ROOT / "backend")!r})
sys.path.insert(0, {str(ROOT)!r})
from agent import sandbox

_real = sandbox.detect
_refusal = subprocess.CompletedProcess(["unshare"], 1, b"", {GITHUB_REFUSAL!r})

def faithful(*a, **k):
    if a or k:
        return _real(*a, **k)
    return _real(which=lambda n: "/usr/bin/unshare",
                 runner=lambda *x, **y: _refusal, use_cache=False)

sandbox.detect = faithful
import tests.test_agent_sandbox as m
suite = unittest.TestLoader().loadTestsFromModule(m)
result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
print("@@RESULT@@" + json.dumps({{
    "available": m.BOUNDARY_AVAILABLE,
    "reason": m.BOUNDARY_REASON,
    "collected": suite.countTestCases(),
    "ran": result.testsRun - len(result.skipped),
    "skipped": len(result.skipped),
    "broken": len(result.failures) + len(result.errors),
}}))
'''
    done = subprocess.run([sys.executable, "-c", program], cwd=ROOT,
                          capture_output=True, text=True)
    if done.returncode:
        return {"error": (done.stdout + done.stderr).strip()[-400:]}
    for line in done.stdout.splitlines():
        if line.startswith("@@RESULT@@"):
            return json.loads(line[len("@@RESULT@@"):])
    return {"error": "the replay produced no result line"}


def read_text(path, fallback="(absent)"):
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return fallback


def git(*args):
    done = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    return done.stdout.strip() if done.returncode == 0 else ""


def tree_state(out_path):
    """Whether the recorded SHA identifies the recorded code.

    The proof file is excluded from its own dirtiness check, because a generated file
    can never be both present and unmodified: it is being written right now. Saying
    "clean" without that caveat would overclaim, and saying "dirty" every time would
    hide the case that actually matters -- other files differing from the commit.
    """
    raw = git("status", "--porcelain")
    if not raw:
        return "نظيفة"
    try:
        rel = str(Path(out_path).resolve().relative_to(ROOT))
    except ValueError:
        rel = None
    others = [line for line in raw.splitlines()
              if not (rel and line[3:].strip().strip('"') == rel)]
    if not others:
        return "نظيفة عدا هذا الملف نفسه (وهو ما يُكتب الآن)"
    return (f"**غير نظيفة** — `{len(others)}` ملفًا خارج هذا الملف تختلف عن الشيفرة "
            f"المسجَّلة، فالـSHA أعلاه لا يحدّد ما جُرّب")


def environment(unshare_path):
    uname = platform.uname()
    cgroup_two = Path("/sys/fs/cgroup/cgroup.controllers").exists()
    version = subprocess.run([unshare_path, "--version"], capture_output=True, text=True)
    return [
        ("النواة", f"{uname.system} {uname.release} ({uname.machine})"),
        ("المستخدم", f"uid {os.getuid()} (غير مميّز)"),
        ("بايثون", platform.python_version()),
        ("unshare", version.stdout.strip() or version.stderr.strip() or "(تعذّر تشغيله)"),
        ("قيد AppArmor على userns",
         read_text("/proc/sys/kernel/apparmor_restrict_unprivileged_userns", "(غير موجود)")),
        ("max_user_namespaces", read_text("/proc/sys/user/max_user_namespaces")),
        ("cgroup", "v2" if cgroup_two else "v1 أو غير معروف"),
        ("علامة حاوية", "نعم" if Path("/.dockerenv").exists() else "لا"),
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default="BOUNDARY-PROOF.md",
                        help="where to write the proof (default: BOUNDARY-PROOF.md)")
    args = parser.parse_args()

    runner = sandbox.detect()
    if not runner.supports():
        reason = getattr(runner, "reason", "incomplete boundary")
        print(f"refusing to write a proof: this host cannot build the boundary.\n"
              f"  runner: {runner.name}\n  reason: {reason}\n"
              f"  A transcript written here would record {PATTERN} skipping, not "
              f"enforcing -- which is the gap, not the evidence.", file=sys.stderr)
        return EXIT_CANNOT_PROVE

    run = run_suite()
    if run["skipped"] or run["failures"] or run["errors"]:
        print(f"refusing to write a proof: the run did not prove it "
              f"({len(run['skipped'])} skipped, {len(run['failures'])} failed, "
              f"{len(run['errors'])} errored).", file=sys.stderr)
        return EXIT_DID_NOT_PROVE
    if not run["enforcement"]:
        print(f"refusing to write a proof: {ENFORCEMENT_CLASS} ran nothing, so a green "
              f"run proves nothing.", file=sys.stderr)
        return EXIT_DID_NOT_PROVE

    head = git("rev-parse", "HEAD")
    commit = git("log", "-1", "--format=%s")
    state = tree_state(args.out)
    unshare_path = getattr(runner, "unshare", None) or "unshare"
    contrast = replay_without_a_boundary()

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    env_rows = "\n".join(f"| {name} | `{value}` |" for name, value in environment(unshare_path))
    capabilities = ", ".join(f"`{c}`" for c in sorted(runner.capabilities)) or "(لا شيء)"
    missing = ", ".join(f"`{m}`" for m in runner.missing()) or "(لا شيء)"

    if "error" in contrast:
        contrast_block = (f"لم تُقَس المقارنة: {contrast['error']}")
    else:
        contrast_block = (
            f"- يُجمع الملف `{contrast['collected']}` اختبارًا.\n"
            f"- تنفيذ: `{contrast['ran']}`، وتُخطّي: **`{contrast['skipped']}`**، "
            f"وفشل: `{contrast['broken']}`.\n"
            f"- السبب المنقول: `{GITHUB_REFUSAL.decode()}`.\n"
            f"- أي أن الحدود هناك **لم تُبنَ**، وهذا الملف يسجّل الـ`{run['ran']}` مقابلها.")

    document = f"""# دليل حدود التنفيذ

**مُولَّد — لا يُحرَّر يدويًا.** أنشأه `scripts/record_boundary_proof.py` في {now}،
على الشيفرة `{head[:9]}` («{commit}»)، وشجرة العمل عند التسجيل {state}.

## ما هذا الملف، وما ليس هو

`ci.yml` يطبع تعليقًا على كل تشغيل يقول إن اختبارات التنفيذ **تُتخطّى** على عدّاء
GitHub المشترك، فالتشغيل هناك يُثبت مسار الرفض ولا يُثبت الفرض. ذلك التعليق صادق،
وهو أيضًا فجوة قائمة: لم يكن في المستودع أي تشغيل بُني فيه الحدود فعلًا ثم هُوجم.

هذا الملف هو ذلك التشغيل.

**وليس** هو: ليس جزءًا من CI، ولا يُغني عن `boundary-proof.yml` (الذي ما زال ملفًا
لم يُشغَّل، ويحتاج عدّاءً مخصّصًا لا وجود له بعد)، ولا يُثبت أن الحدود تُبنى على كل
مضيف — بل على المضيف الموصوف أدناه وحده. وما زال `C25` عند `Partial`: سقف الموارد
الكلي بـcgroup ناقص كما هو.

## البيئة التي سُجّل عليها

| البند | القيمة |
|---|---|
{env_rows}

## الحدود التي بُنيت فعلًا

| البند | القيمة |
|---|---|
| العدّاء | `{runner.name}` |
| القدرات المُعلنة | {capabilities} |
| الناقص | {missing} |
| عزل نظام الملفات | {"نعم" if runner.filesystem_isolated else "**لا**"} |

## النتيجة

| القياس | العدد |
|---|---|
| اختبارات مجموعة في `{PATTERN}` | `{run['collected']}` |
| **نُفّذت** | **`{run['ran']}`** |
| **تُخطّيت** | **`{len(run['skipped'])}`** |
| فشل / أخطاء | `{len(run['failures'])}` / `{len(run['errors'])}` |
| منها في `{ENFORCEMENT_CLASS}` | `{len(run['enforcement'])}` (كلها نُفّذت) |

صفر تخطٍّ هو الفرق بين هذا الملف وتعليق CI: هناك `{contrast.get('ran', '?')}` اختبارًا
تُثبت الرفض، وهنا `{run['ran']}` تُثبت الفرض أيضًا.

## المقارنة مع عدّاء GitHub

مقيسة بإعادة تشغيل `detect()` بنص الرفض نفسه الذي ينقله التعليق، في مفسّر جديد:

{contrast_block}

## السجل الكامل

```text
{run['log'].rstrip()}
```

## إعادة الإنتاج

```bash
python scripts/record_boundary_proof.py --out BOUNDARY-PROOF.md
```

على مضيف يمنع user namespaces يخرج السكربت بـ`{EXIT_CANNOT_PROVE}` **ولا يكتب ملفًا**:
دليل مكتوب حيث تُتخطّى الاختبارات هو نفس الكذبة التي يرفضها `ci.yml`.

## حدود هذا الدليل

- مضيف واحد ونواة واحدة ({platform.uname().release}): يُثبت أن الاختبارات قابلة للتكذيب
  وأن الحدود تصمد هنا، ولا يقول شيئًا عن مضيف آخر.
- تاريخ التسجيل جزء من الدليل: القيد في النواة، والنواة تتغيّر.
- لا يُغلق سقف الموارد الكلي (`C25` يبقى `Partial`)، ولا يُشغّل فحص الطفرة.
"""
    Path(args.out).write_text(document, encoding="utf-8")
    print(f"wrote {args.out}: {run['ran']} ran, 0 skipped, "
          f"{len(run['enforcement'])} enforcement tests")
    return 0


if __name__ == "__main__":
    sys.exit(main())
