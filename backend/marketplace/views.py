"""Server-rendered Arabic HTML for the Marketplace dashboard.

The page is rendered in Python rather than built by client-side JavaScript for
one reason: everything it shows is already on the server, and shipping it to a
browser to be assembled would mean a second, unauthenticated API surface for the
same data. No script runs here at all, which also keeps it inside the
application's CSP (``script-src 'self'``) without an exception.

Nothing in this module renders a credential. Tokens arrive as fingerprints from
``store.public_resource`` and stay that way -- a dashboard that could display a
token would be the shortest path to leaking one, and rotation exists precisely
so nobody needs to see the current value.
"""
from __future__ import annotations

import html
from typing import Mapping, Optional, Sequence

STATUS_LABELS = {
    "active": "نشط",
    "uninstalled": "مُزال",
    "ready": "جاهز",
    "pending": "قيد التحضير",
    "onboarding": "قيد الإعداد",
    "suspended": "موقوف",
    "resumed": "مُستأنف",
    "error": "خطأ",
}

EVENT_LABELS = {
    "integration-configuration.removed": "إزالة التثبيت",
    "integration-configuration.permission-upgraded": "ترقية الصلاحيات",
    "integration-configuration.scope-change-confirmed": "تأكيد تغيير النطاق",
    "integration-configuration.transferred": "نقل التثبيت",
    "deployment.created": "إنشاء نشر",
    "deployment.succeeded": "نجاح النشر",
    "deployment.error": "فشل النشر",
    "deployment.canceled": "إلغاء النشر",
    "project.created": "إنشاء مشروع",
    "project.removed": "حذف مشروع",
    "domain.created": "إنشاء نطاق",
}

HANDLED_LABELS = {"uninstalled": "أُزيل التثبيت", "recorded": "مُسجَّل", "": "—"}


def _esc(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _label(mapping: Mapping, key, fallback=None) -> str:
    return mapping.get(key) or (fallback if fallback is not None else _esc(key))


def _rows(values: Sequence[str]) -> str:
    if not values:
        return '<p class="empty">لا شيء بعد.</p>'
    items = "".join(f"<li>{_esc(value)}</li>" for value in values)
    return f'<ul class="tight">{items}</ul>'


def render_resource(resource: Mapping, env_keys: Sequence[str]) -> str:
    fingerprint = resource.get("tokenFingerprint") or "—"
    metadata = resource.get("metadata") or {}
    lines = [
        f'<li><span>الحالة</span><b>{_label(STATUS_LABELS, resource.get("status"))}</b></li>',
        f'<li><span>مساحة العمل</span><code>{_esc(resource.get("workspaceId"))}</code></li>',
        f'<li><span>بصمة الرمز</span><code>{_esc(fingerprint)}</code></li>',
        f'<li><span>الخطة</span><b>{_esc(resource.get("billingPlanId"))}</b></li>',
    ]
    if metadata.get("workspace_name"):
        lines.append(f'<li><span>الاسم الداخلي</span><b>{_esc(metadata["workspace_name"])}</b></li>')
    if metadata.get("focus"):
        lines.append(f'<li><span>المجال</span><b>{_esc(metadata["focus"])}</b></li>')
    injected = "".join(f"<code>{_esc(key)}</code> " for key in env_keys)
    return f"""
    <article class="card">
      <header>
        <h3>{_esc(resource.get("name"))}</h3>
        <span class="chip" data-state="{_esc(resource.get("status"))}">{
            _label(STATUS_LABELS, resource.get("status"))}</span>
      </header>
      <ul class="facts">{''.join(lines)}</ul>
      <p class="hint">متغيرات البيئة المحقونة في المشروع: {injected}</p>
      <p class="hint">الرمز نفسه لا يظهر هنا أبدًا؛ لتغييره استخدم زر «تدوير الرمز»
        في Vercel، وهو يستدعي مسار التدوير في هذا الخادم.</p>
    </article>"""


def render_installation(installation: Mapping, env_keys: Sequence[str]) -> str:
    resources = installation.get("resources") or []
    cards = "".join(render_resource(resource, env_keys) for resource in resources) or (
        '<p class="empty">لا موارد بعد. أنشئ مساحة عمل من تبويب Storage في Vercel.</p>')
    return f"""
    <section class="panel">
      <header class="panel-head">
        <h2>{_esc(installation.get("account_name") or "حساب بدون اسم")}</h2>
        <span class="chip" data-state="{_esc(installation.get("status"))}">{
            _label(STATUS_LABELS, installation.get("status"))}</span>
      </header>
      <ul class="facts">
        <li><span>معرّف التثبيت</span><code>{_esc(installation.get("id"))}</code></li>
        <li><span>الفريق</span><code>{_esc(installation.get("team_id") or "—")}</code></li>
        <li><span>بصمة رمز الوصول</span><code>{
            _esc(installation.get("token_fingerprint") or "—")}</code></li>
        <li><span>عدد الموارد</span><b>{len(resources)}</b></li>
      </ul>
      <div class="cards">{cards}</div>
    </section>"""


def render_events(events: Sequence[Mapping]) -> str:
    if not events:
        return '<p class="empty">لم يصل أي حدث بعد.</p>'
    rows = "".join(
        f"<tr><td>{_label(EVENT_LABELS, event.get('type'))}</td>"
        f"<td><code>{_esc(event.get('installation_id') or '—')}</code></td>"
        f"<td>{_label(HANDLED_LABELS, event.get('handled'), '—')}</td>"
        f"<td>{_esc(event.get('created_at') or '—')}</td></tr>"
        for event in events)
    return f"""
    <table class="events">
      <thead><tr><th>الحدث</th><th>التثبيت</th><th>المعالجة</th><th>التوقيت</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>"""


def render_message(title: str, message: str, code: Optional[str] = None,
                   settings=None) -> str:
    """One Arabic card with one explanation. Used for every refusal.

    Deliberately not an alert or a redirect: the person reading it arrived from
    Vercel's dashboard and needs to know what to do next, and the ``code`` is
    printed because it is the string a support thread and a log line can both
    be searched for.
    """
    code_line = ""
    if code:
        code_line = f'<p class="hint">رمز الحالة: <code>{_esc(code)}</code></p>'
    product = _esc(getattr(settings, "product_id", "")) if settings else ""
    return f"""<!doctype html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="color-scheme" content="light dark">
  <meta name="robots" content="noindex,nofollow">
  <title>{_esc(title)} · واحة</title>
  <link rel="stylesheet" href="/static/marketplace.css">
</head>
<body>
<div class="shell">
  <header class="top">
    <div>
      <div class="crumb">واحة / Vercel Marketplace</div>
      <h1>{_esc(title)}</h1>
    </div>
    <div class="crumb"><a href="/">العودة إلى واحة</a></div>
  </header>
  <section class="panel">
    <p class="notice">{_esc(message)}</p>
    {code_line}
  </section>
  {f'<ul class="facts"><li><span>المنتج</span><code>{product}</code></li></ul>' if product else ''}
</div>
</body>
</html>
"""


def render_dashboard(state: Mapping, settings) -> str:
    """The whole page. ``settings`` supplies only names of variables."""
    missing = settings.missing() if hasattr(settings, "missing") else ()
    configured = bool(state.get("configured"))
    status_chip = "مُهيّأ" if configured else "غير مُهيّأ"
    missing_block = ""
    if missing:
        missing_block = ("<p class='warn'>متغيرات البيئة الناقصة على الخادم: "
                         + " ".join(f"<code>{_esc(name)}</code>" for name in missing)
                         + "</p>")
    installations = state.get("installations") or []
    body = "".join(render_installation(item, settings.env_keys())
                   for item in installations) or (
        '<p class="empty">لا تثبيتات بعد. بعد تثبيت التكامل من متجر Vercel سيظهر هنا.</p>')
    events = state.get("events") or []
    return f"""<!doctype html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="color-scheme" content="light dark">
  <meta name="robots" content="noindex,nofollow">
  <title>واحة · Vercel Marketplace</title>
  <link rel="stylesheet" href="/static/marketplace.css">
</head>
<body>
<div class="shell">
  <header class="top">
    <div>
      <div class="crumb">واحة / Vercel Marketplace</div>
      <h1>تكامل متجر Vercel</h1>
    </div>
    <div class="crumb"><a href="/">العودة إلى واحة</a></div>
  </header>

  <p class="notice">
    هذه اللوحة تقرأ ما خزّنه الخادم عن تثبيتات متجر Vercel ومواردها. الأسرار — رمز وصول
    التثبيت ورمز الواجهة البرمجية لكل مساحة عمل — <b>لا تظهر هنا ولا تُنقل إلى المتصفح</b>؛
    ما يظهر هو بصمة لكل رمز تكفي للتمييز بين رمزين بعد التدوير.
  </p>

  <section class="panel">
    <header class="panel-head">
      <h2>حالة التكامل</h2>
      <span class="chip" data-state="{'ready' if configured else 'error'}">{status_chip}</span>
    </header>
    <ul class="facts">
      <li><span>المنتج</span><code>{_esc(getattr(settings, 'product_id', ''))}</code></li>
      <li><span>العنوان الأساسي</span><code>{_esc(getattr(settings, 'base_url', '') or '—')}</code></li>
      <li><span>مسار الويب هوك</span><code>{_esc(getattr(settings, 'webhook_path', '/v1/webhooks/vercel'))}</code></li>
    </ul>
    {missing_block}
  </section>

  {body}

  <section class="panel">
    <h2>آخر أحداث الويب هوك</h2>
    {render_events(events)}
  </section>
</div>
</body>
</html>
"""
