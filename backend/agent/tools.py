"""Tool registry and the built-in tools.

Design rules, all enforced here rather than in the prompt:

* No shell, no code execution, no filesystem writes, no environment reads. The
  free Render tier gives no sandbox, so pretending otherwise would be the
  dangerous option; generated code is produced as *artifacts* and previewed in
  a sandboxed iframe by the client instead.
* Anything that leaves the server (web_fetch) is capability-gated by the
  operator AND requires a human approval per task unless the operator opted into
  auto-approval for read-only tools.
* Results are size-capped and JSON-serialisable, so a task can never store a
  megabyte of junk in the free Neon database.
"""
import ast
import ipaddress
import json
import math
import operator
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

MAX_FETCH_BYTES = 200_000
MAX_RESULT_CHARS = 6000
TEXT_CONTENT_TYPES = ("text/", "application/json", "application/xml", "+json", "+xml")
ARTIFACT_KINDS = {"html": "text/html", "css": "text/css", "javascript": "text/javascript",
                  "markdown": "text/markdown", "json": "application/json"}
ARTIFACT_NAME = re.compile(r"^[A-Za-z0-9_\u0600-\u06FF][A-Za-z0-9_.\u0600-\u06FF-]{0,63}$")


class ToolError(Exception):
    """A recoverable, user-presentable tool failure. The loop records it and
    lets the model react instead of killing the task."""


# --- calculator ---------------------------------------------------------------
_ALLOWED_NODES = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant, ast.Call, ast.Name,
                  ast.Load, ast.operator, ast.unaryop)
_BINOPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
           ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
           ast.Pow: operator.pow}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCTIONS = {"round": round, "abs": abs, "min": min, "max": max,
              "sqrt": math.sqrt, "floor": math.floor, "ceil": math.ceil}
_CONSTANTS = {"pi": math.pi, "e": math.e}


def _eval_node(node):
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        raise ToolError("الأرقام فقط داخل الآلة الحاسبة.")
    if isinstance(node, ast.BinOp):
        handler = _BINOPS.get(type(node.op))
        if handler is None:
            raise ToolError("عملية غير مدعومة في الآلة الحاسبة.")
        left, right = _eval_node(node.left), _eval_node(node.right)
        if isinstance(node.op, ast.Pow) and (abs(right) > 64 or abs(left) > 1e6):
            raise ToolError("الأُس خارج الحدود المسموحة.")
        try:
            return handler(left, right)
        except (ZeroDivisionError, OverflowError, ValueError) as error:
            raise ToolError("عملية غير معرفة (قسمة على صفر أو تكبير مفرط).") from error
    if isinstance(node, ast.UnaryOp):
        handler = _UNARY.get(type(node.op))
        if handler is None:
            raise ToolError("إشارة غير مدعومة.")
        return handler(_eval_node(node.operand))
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCTIONS:
            raise ToolError("الدالة غير مسموحة في الآلة الحاسبة.")
        if node.keywords:
            raise ToolError("وسائط الأسماء (keywords) غير مدعومة.")
        return _FUNCTIONS[node.func.id](*[_eval_node(arg) for arg in node.args])
    if isinstance(node, ast.Name):
        if node.id in _CONSTANTS:
            return _CONSTANTS[node.id]
        raise ToolError("اسم غير معروف في الآلة الحاسبة.")
    raise ToolError("تعبير غير مدعوم.")


def _calculator(ctx, args):
    """Evaluate a numeric expression with a whitelisted AST walk.

    Only numbers, the four arithmetic operators (plus // % **), the fixed
    function table and two named constants are reachable. There is no attribute
    access, no subscript, no string literal used as code and no import, so the
    usual `__class__`/`os.system` payloads fail closed.
    """
    expression = str(args.get("expression", "")).strip()
    if not expression or len(expression) > 400:
        raise ToolError("اكتب تعبيراً رياضياً أقصر من 400 حرف.")
    if '"' in expression or "'" in expression or "__" in expression:
        raise ToolError("الآلة الحاسبة تأخذ تعبيراً رقمياً فقط (بلا نصوص أو أسماء داخلية).")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as error:
        raise ToolError("تعبير غير صالح: " + str(error.msg)) from error
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise ToolError("عناصر غير مسموحة في التعبير.")
    value = _eval_node(tree)
    if isinstance(value, float) and not math.isfinite(value):
        raise ToolError("النتيجة غير منتهية.")
    return {"expression": expression, "result": value}


def _clock(ctx, args):
    now = time.time()
    offset_days = float(args.get("offset_days", 0) or 0)
    if abs(offset_days) > 3650:
        raise ToolError("الإزاحة يجب أن تكون أقل من 10 سنوات.")
    moment = now + offset_days * 86400
    return {"unix": round(moment, 3), "iso_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(moment)),
            "date": time.strftime("%Y-%m-%d", time.gmtime(moment)),
            "weekday_en": time.strftime("%A", time.gmtime(moment)),
            "offset_days_applied": offset_days}


def _skill_lookup(ctx, args):
    query = str(args.get("query", "")).strip().lower()
    skill_id = str(args.get("skill_id", "")).strip()
    skills = ctx.skills or []
    if skill_id:
        match = next((item for item in skills if item.get("id") == skill_id), None)
        if not match:
            raise ToolError("هذه المهارة غير موجودة في الكتالوج.")
        return {"skill": _skill_brief(match), "matched_by": "id"}
    if not query:
        raise ToolError("اكتب كلمة بحث أو معرّف مهارة.")
    scored = []
    for item in skills:
        haystack = " ".join([item.get("name", ""), item.get("description", ""),
                             " ".join(item.get("tags", []))]).lower()
        if query in haystack:
            scored.append(item)
    if not scored:
        return {"skills": [], "matched_by": "none",
                "hint": "لا مهارة مطابقة؛ استخدم معرفتك أو اطلب صياغة أخرى."}
    return {"skills": [_skill_brief(item) for item in scored[:5]], "matched_by": "query"}


def _skill_brief(skill):
    return {"id": skill.get("id"), "name": skill.get("name"),
            "description": str(skill.get("description", ""))[:300],
            "category": skill.get("category"), "difficulty": skill.get("difficulty"),
            "steps": [str(step)[:200] for step in (skill.get("steps") or [])[:8]]}


def _memory_write(ctx, args):
    content = str(args.get("content", "")).strip()
    kind = str(args.get("kind", "note")).strip() or "note"
    if kind not in ("note", "preference", "goal"):
        raise ToolError("نوع الذاكرة يجب أن يكون note أو preference أو goal.")
    if not 3 <= len(content) <= 400:
        raise ToolError("الملاحظة يجب أن تكون بين 3 و400 حرف.")
    saved = ctx.store.remember(ctx.user_id, kind, content, limit=ctx.config.MEMORY_MAX_ITEMS)
    return {"saved": True, "memory_id": saved, "items": saved and ctx.store.memory_count(ctx.user_id)}


def _artifact_write(ctx, args):
    name = str(args.get("name", "")).strip()
    kind = str(args.get("kind", "html")).strip().lower()
    content = str(args.get("content", ""))
    if kind not in ARTIFACT_KINDS:
        raise ToolError("أنواع الملفات المسموحة: " + ", ".join(sorted(ARTIFACT_KINDS)) + ".")
    if not ARTIFACT_NAME.match(name):
        raise ToolError("اسم الملف يجب أن يكون قصيراً وبلا مسارات أو شرطات مائلة.")
    if not content.strip():
        raise ToolError("لا يمكن حفظ ملف فارغ.")
    if len(content.encode("utf-8")) > ctx.config.MAX_ARTIFACT_BYTES:
        raise ToolError("الملف أكبر من الحد المسموح في الخطة المجانية.")
    if kind == "html" and re.search(r"<\s*script[^>]*\s*src\s*=", content, re.I):
        raise ToolError("لا تُقبل سكربتات خارجية داخل المعاينة؛ ضع الكود داخل الملف نفسه.")
    artifact_id = ctx.store.put_artifact(ctx.task_id, ctx.user_id, name, kind, content)
    return {"artifact_id": artifact_id, "name": name, "kind": kind,
            "bytes": len(content.encode("utf-8")),
            "preview": "العميل يعرض هذا الملف داخل iframe معزول (بدون نفس الأصل)."}


# --- web_fetch ----------------------------------------------------------------
def _is_public_address(host):
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as error:
        raise ToolError("تعذّر حلّ اسم النطاق.") from error
    addresses = {info[4][0] for info in infos}
    if not addresses:
        raise ToolError("لا عنوان لهذا النطاق.")
    for raw in addresses:
        try:
            ip = ipaddress.ip_address(raw.split("%")[0])
        except ValueError as error:
            raise ToolError("عنوان غير صالح.") from error
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified or getattr(ip, "is_site_local", False)):
            raise ToolError("هذا العنوان داخلي أو محجوب؛ الجلب مسموح للعموم فقط.")
    return True


def _strip_html(html):
    html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?s)<!--.*?-->", " ", html)
    html = re.sub(r"(?i)<br\s*/?>", "\n", html)
    html = re.sub(r"(?i)</(p|div|li|h[1-6]|tr)>", "\n", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
            .replace("&gt;", ">").replace("&#39;", "'").replace("&quot;", '"'))
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line)


def _web_fetch(ctx, args):
    if not ctx.config.NETWORK_TOOLS:
        raise ToolError("أدوات الشبكة معطّلة على هذا الخادم (AGENT_NETWORK_TOOLS=0).")
    raw = str(args.get("url", "")).strip()
    parts = urllib.parse.urlsplit(raw)
    if parts.scheme not in ("http", "https"):
        raise ToolError("المسموح http و https فقط.")
    if not parts.hostname:
        raise ToolError("رابط بلا نطاق.")
    if parts.username or parts.password:
        raise ToolError("لا تُقبل بيانات اعتماد داخل الرابط.")
    if parts.port not in (None, 80, 443):
        raise ToolError("المنافذ المسموحة 80 و 443 فقط.")
    _is_public_address(parts.hostname)
    url = urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path or "/",
                                   parts.query, ""))
    headers = {"User-Agent": "WahaAgent/0.1 (learning catalog; +https://github.com/sayedelazameydesign-crypto/1pro)",
               "Accept": "text/markdown, text/html, text/plain, application/json",
               "Cookie": "", "Cookie2": ""}
    body = None
    final_url = url
    for _ in range(4):
        request = urllib.request.Request(final_url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=min(15, ctx.config.PROVIDER_TIMEOUT_SECONDS)) as response:
                content_type = (response.headers.get("Content-Type") or "").lower()
                body = response.read(MAX_FETCH_BYTES + 1)
                if len(body) > MAX_FETCH_BYTES:
                    raise ToolError("المحتوى أكبر من الحد المسموح (200KB).")
                break
        except urllib.error.HTTPError as error:
            if error.code in (301, 302, 303, 307, 308):
                target = error.headers.get("Location")
                if not target:
                    raise ToolError("تحويل بلا وجهة.")
                nxt = urllib.parse.urljoin(final_url, target)
                nxt_parts = urllib.parse.urlsplit(nxt)
                if nxt_parts.scheme not in ("http", "https") or nxt_parts.port not in (None, 80, 443):
                    raise ToolError("التحويل يشير إلى عنوان غير مسموح.")
                if nxt_parts.hostname != parts.hostname:
                    _is_public_address(nxt_parts.hostname or "")
                final_url = nxt
                continue
            if error.code in (401, 403, 404):
                raise ToolError(f"الصفحة غير متاحة (HTTP {error.code}).") from error
            raise ToolError(f"فشل الجلب (HTTP {error.code}).") from error
        except (TimeoutError, socket.timeout) as error:
            raise ToolError("انتهت مهلة الجلب.") from error
        except (urllib.error.URLError, OSError) as error:
            raise ToolError("تعذّر الوصول إلى العنوان.") from error
    else:
        raise ToolError("تحويلات كثيرة جداً.")
    if content_type and not any(token in content_type for token in TEXT_CONTENT_TYPES):
        raise ToolError("هذا ليس نصاً (" + content_type[:60] + ").")
    text = body.decode("utf-8", "replace")
    if "html" in content_type or text.lstrip()[:1] == "<":
        text = _strip_html(text)
    truncated = len(text) > MAX_RESULT_CHARS
    return {"url": final_url, "content_type": content_type[:80],
            "text": text[:MAX_RESULT_CHARS], "truncated": truncated,
            "fetched_at": int(time.time())}


class Tool:
    def __init__(self, name, description, args, handler, requires_approval=False,
                 read_only=True, network=False, gated=None):
        self.name = name
        self.description = description
        self.args = args
        self.handler = handler
        self.requires_approval = requires_approval
        self.read_only = read_only
        self.network = network
        self.gated = gated  # config attribute name that must be truthy

    def enabled(self, config):
        return self.gated is None or bool(getattr(config, self.gated, True))

    def brief(self):
        return {"name": self.name, "description": self.description, "args": self.args,
                "requires_approval": self.requires_approval, "read_only": self.read_only,
                "network": self.network}

    def validate(self, args):
        if not isinstance(args, dict):
            raise ToolError("وسائط الأداة يجب أن تكون كائناً.")
        for key, spec in self.args.items():
            value = args.get(key)
            if spec.get("required") and (value is None or value == ""):
                raise ToolError(f"الحقل `{key}` مطلوب لـ {self.name}.")
            if value is None:
                continue
            if spec.get("type") == "string" and not isinstance(value, str):
                raise ToolError(f"الحقل `{key}` يجب أن يكون نصاً.")
            if spec.get("type") == "number" and not isinstance(value, (int, float)):
                raise ToolError(f"الحقل `{key}` يجب أن يكون رقماً.")
        unknown = set(args) - set(self.args)
        if unknown:
            raise ToolError("حقول غير معروفة: " + ", ".join(sorted(unknown)))
        return args


class Registry:
    def __init__(self, tools=None):
        self._tools = {}
        for tool in tools or []:
            self.register(tool)

    def register(self, tool):
        self._tools[tool.name] = tool

    def names(self, config=None):
        if config is None:
            return sorted(self._tools)
        return sorted(name for name, tool in self._tools.items() if tool.enabled(config))

    def get(self, name, config=None):
        tool = self._tools.get(name)
        if tool is None:
            raise ToolError(f"الأداة `{name}` غير معروفة.")
        if not tool.enabled(config):
            raise ToolError(f"الأداة `{name}` معطّلة على هذا الخادم.")
        return tool

    def available(self, config):
        return [tool.brief() for name, tool in sorted(self._tools.items()) if tool.enabled(config)]

    def prompt_text(self, config):
        lines = []
        for name, tool in sorted(self._tools.items()):
            if not tool.enabled(config):
                continue
            args = ", ".join(f"{key}: {spec.get('type', 'string')}"
                             + ("" if spec.get("required") else "?")
                             for key, spec in tool.args.items())
            flags = "read-only" if tool.read_only else "writes"
            if tool.requires_approval:
                flags += ", needs user approval"
            lines.append(f"- {name}({args}) — {tool.description} [{flags}]")
        return "\n".join(lines) or "- (no tools enabled)"


def build_registry():
    return Registry([
        Tool("calculator", "احسب تعبيراً رياضياً بأمان (أرقام، +-*/ ^، round/sqrt/min/max).",
             {"expression": {"type": "string", "required": True,
                             "description": "تعبير مثل: round(sqrt(2)*pi, 4)"}},
             _calculator),
        Tool("clock", "التاريخ والوقت الحالي بتوقيت UTC، مع إزاحة اختيارية بالأيام.",
             {"offset_days": {"type": "number", "required": False,
                             "description": "عدد أيام موجب أو سالب"}},
             _clock),
        Tool("skill_lookup", "ابحث في كتالوج مهارات واحة عن مهارة أو خطوات معلّمة.",
             {"query": {"type": "string", "required": False, "description": "كلمة بحث"},
              "skill_id": {"type": "string", "required": False, "description": "معرّف مثل SKL002"}},
             _skill_lookup),
        Tool("memory_write", "احفظ ملاحظة قصيرة عن تفضيلات المتعلّم أو أهدافه لجلسات لاحقة.",
             {"content": {"type": "string", "required": True,
                          "description": "نص قصير حتى 400 حرف"},
              "kind": {"type": "string", "required": False,
                       "description": "note | preference | goal"}},
             _memory_write, requires_approval=False, read_only=False),
        Tool("artifact_write", "أنشئ أو حدّث ملفاً في لوحة العرض (HTML/CSS/JS/JSON/Markdown) للمعاينة.",
             {"name": {"type": "string", "required": True, "description": "اسم مثل index.html"},
              "kind": {"type": "string", "required": True, "description": ", ".join(sorted(ARTIFACT_KINDS))},
              "content": {"type": "string", "required": True, "description": "محتوى الملف"}},
             _artifact_write, requires_approval=False, read_only=False),
        Tool("web_fetch", "اجلب نص صفحة ويب عامة عبر GET مع حراسة ضد العناوين الداخلية.",
             {"url": {"type": "string", "required": True, "description": "https://…"}},
             _web_fetch, requires_approval=True, read_only=True, network=True,
             gated="NETWORK_TOOLS"),
    ])
