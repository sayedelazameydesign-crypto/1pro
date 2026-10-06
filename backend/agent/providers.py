"""Provider interface.

The core never talks to a vendor directly: it asks for a named provider and
receives text (or a stream of deltas). That keeps Gemini, NVIDIA and any future
local model interchangeable, and keeps "no silent fallback" enforceable: the
provider chosen at task creation is the provider used for every step.

Implemented here:
  * GeminiProvider   - Google generativelanguage REST, direct with GEMINI_API_KEY.
  - GatewayProvider  - the same calls routed through the PromptQL platform API
                       (visitor token supplies the credential, never the server).
  * FakeProvider     - deterministic, offline; used by the test-suite and by
                       `AGENT_FAKE_PROVIDER=1` smoke runs.
"""
import json
import socket
import urllib.error
import urllib.request

MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class ProviderError(Exception):
    """A user-presentable failure with a stable machine code."""

    def __init__(self, message, status=502, code="ai_unavailable", retry_after=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.retry_after = retry_after


class Result:
    def __init__(self, text, usage=None, provider=None, model=None):
        self.text = text
        self.usage = usage or {}
        self.provider = provider
        self.model = model


class BaseProvider:
    name = "base"
    label = "Model"
    streaming = True
    json_mode = False          # can the provider be forced to return strict JSON?
    personal_connection = False

    def __init__(self, model):
        self.model = model

    def complete(self, system, messages, max_output_tokens=1200, temperature=0.4, json_mode=False):
        raise NotImplementedError

    def stream(self, system, messages, max_output_tokens=1200, temperature=0.4):
        # Default: a single chunk. Providers with real streaming override this.
        yield self.complete(system, messages, max_output_tokens, temperature).text


def _parse_retry_after(headers):
    raw = (headers.get("Retry-After") if headers else "") or ""
    return int(raw) if raw.strip().isdigit() else None


def _decode_openai_payload(body):
    data = json.loads(body)
    try:
        text = data["choices"][0]["message"].get("content", "")
    except (KeyError, IndexError, TypeError) as error:
        raise ProviderError("لم يُرجع الموفّر نصاً صالحاً. عدّل الطلب وحاول مرة أخرى.",
                            502, "ai_empty") from error
    usage = {key: value for key, value in (data.get("usage") or {}).items()
             if key in ("prompt_tokens", "completion_tokens") and isinstance(value, int)}
    if not isinstance(text, str) or not text.strip():
        raise ProviderError("لم يُرجع الموفّر نصاً صالحاً. عدّل الطلب وحاول مرة أخرى.",
                            502, "ai_empty")
    return text, usage


class HttpProvider(BaseProvider):
    """Shared HTTP + error-mapping behaviour for remote providers."""

    def _post(self, url, headers, payload, timeout):
        request = urllib.request.Request(url, method="POST", data=json.dumps(payload).encode(),
                                         headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response
        except urllib.error.HTTPError as error:
            code = error.code
            retry_after = _parse_retry_after(error.headers)
            if code in (401, 403):
                raise ProviderError(
                    f"الوصول إلى {self.label} غير متاح. تحقق من المفتاح أو اتصال الحساب؛ "
                    "لا يتم التحويل إلى موفّر آخر.", 403, "ai_permission") from error
            if code in (402, 429):
                raise ProviderError(
                    f"بلغت {self.label} حد الطلبات أو الحصة. انتظر وراجع حصة الحساب؛ "
                    "لم يتم استخدام موفّر بديل.", 429, "ai_rate_limit", retry_after) from error
            raise ProviderError(f"خدمة {self.label} غير متاحة حالياً. حاول لاحقاً.") from error
        except (TimeoutError, socket.timeout) as error:
            raise ProviderError(f"انتهت مهلة {self.label}. لم تُحفظ نتيجة ناقصة؛ حاول مرة أخرى.",
                                504, "ai_timeout") from error
        except (urllib.error.URLError, OSError) as error:
            raise ProviderError(f"تعذّر الاتصال بـ{self.label}. حاول مرة أخرى.") from error

    @staticmethod
    def _read(response):
        body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ProviderError("ردّ الموفّر أكبر من الحد المسموح؛ اختصر طلبك.", 502, "ai_too_large")
        return body


class GeminiProvider(HttpProvider):
    """Direct Google Gemini REST usage (x-goog-api-key), no gateway involved."""

    name = "gemini"
    label = "Gemini"
    json_mode = True
    base_url = "https://generativelanguage.googleapis.com/v1beta/models"

    personal_connection = False

    def __init__(self, model, api_key, timeout=75):
        super().__init__(model)
        self.api_key = api_key
        self.timeout = timeout

    def _payload(self, system, messages, max_output_tokens, temperature, json_mode):
        contents = [{"role": "model" if m["role"] == "assistant" else "user",
                     "parts": [{"text": m["content"]}]} for m in messages]
        generation = {"maxOutputTokens": max_output_tokens, "temperature": temperature}
        if json_mode and self.json_mode:
            generation["responseMimeType"] = "application/json"
        payload = {"contents": contents, "generationConfig": generation}
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        return payload

    def complete(self, system, messages, max_output_tokens=1200, temperature=0.4, json_mode=False):
        payload = self._payload(system, messages, max_output_tokens, temperature, json_mode)
        url = f"{self.base_url}/{self.model}:generateContent"
        headers = {"Content-Type": "application/json", "Accept": "application/json",
                   "x-goog-api-key": self.api_key}
        with self._post(url, headers, payload, self.timeout) as response:
            body = self._read(response)
        return Result(*self._extract(body), provider=self.name, model=self.model)

    @staticmethod
    def _extract(body):
        try:
            data = json.loads(body)
            parts = data["candidates"][0].get("content", {}).get("parts", [])
            text = "".join(part.get("text", "") for part in parts if not part.get("thought"))
            usage_meta = data.get("usageMetadata") or {}
            usage = {}
            if isinstance(usage_meta.get("promptTokenCount"), int):
                usage["prompt_tokens"] = usage_meta["promptTokenCount"]
            if isinstance(usage_meta.get("candidatesTokenCount"), int):
                usage["completion_tokens"] = usage_meta["candidatesTokenCount"]
        except (ValueError, IndexError, TypeError, KeyError) as error:
            raise ProviderError("لم يُرجع Gemini نصاً صالحاً. عدّل سؤالك وحاول مرة أخرى.",
                                502, "ai_empty") from error
        if not text.strip():
            raise ProviderError("لم يُرجع Gemini نصاً صالحاً. عدّل سؤالك وحاول مرة أخرى.",
                                502, "ai_empty")
        return text.strip(), usage

    def stream(self, system, messages, max_output_tokens=1200, temperature=0.4):
        payload = self._payload(system, messages, max_output_tokens, temperature, False)
        url = f"{self.base_url}/{self.model}:streamGenerateContent?alt=sse"
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream",
                   "x-goog-api-key": self.api_key}
        with self._post(url, headers, payload, self.timeout) as response:
            for chunk in _iter_sse(response):
                if not chunk:
                    continue
                try:
                    data = json.loads(chunk)
                    parts = data["candidates"][0].get("content", {}).get("parts", [])
                except (ValueError, IndexError, KeyError, TypeError):
                    continue
                for part in parts:
                    text = part.get("text")
                    if text and not part.get("thought"):
                        yield text


class GatewayProvider(HttpProvider):
    """Same models routed through the PromptQL platform API.

    The credential stays the visitor's own token; the server never holds a key.
    Both the `gemini` and `nvidia` integrations use this path, which is why the
    standalone deploy advertises Gemini only. Streaming is not offered here:
    the gateway buffers the response, so `streaming` stays False and the UI
    falls back to polling task events.
    """

    label = "PromptQL"
    streaming = False

    def __init__(self, name, model, url, visitor_token, wire="gemini",
                 label=None, timeout=75):
        super().__init__(model)
        self.name = name
        self.url = url
        self.visitor_token = visitor_token
        self.wire = wire
        self.label = label or name
        self.timeout = timeout

    def complete(self, system, messages, max_output_tokens=1200, temperature=0.4, json_mode=False):
        if self.wire == "openai":
            payload = {"model": self.model, "max_tokens": max_output_tokens,
                       "temperature": temperature, "stream": False,
                       "messages": ([{"role": "system", "content": system}] if system else [])
                       + [{"role": item["role"], "content": item["content"]} for item in messages]}
        else:
            contents = [{"role": "model" if item["role"] == "assistant" else "user",
                         "parts": [{"text": item["content"]}]} for item in messages]
            payload = {"contents": contents,
                       "generationConfig": {"maxOutputTokens": max_output_tokens,
                                            "temperature": temperature}}
            if system:
                payload["systemInstruction"] = {"parts": [{"text": system}]}
        headers = {"Content-Type": "application/json", "Accept": "application/json",
                   "Authorization": "Bearer " + self.visitor_token,
                   "X-PromptQL-Description": f"Waha agent step with {self.label}"}
        with self._post(self.url, headers, payload, self.timeout) as response:
            body = self._read(response)
        if self.wire == "openai":
            text, usage = _decode_openai_payload(body)
        else:
            text, usage = GeminiProvider._extract(body)
        return Result(text, usage, provider=self.name, model=self.model)


class FakeProvider(BaseProvider):
    """Deterministic provider for tests and for offline smoke runs."""

    name = "fake"
    label = "Fake"
    streaming = False
    json_mode = True

    def __init__(self, model="fake-model", script=None):
        super().__init__(model)
        self.script = list(script or [])
        self.calls = []

    def complete(self, system, messages, max_output_tokens=1200, temperature=0.4, json_mode=False):
        self.calls.append({"system": system, "messages": list(messages)})
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, ProviderError):
                raise item
            if isinstance(item, Exception):
                raise item
            if isinstance(item, dict):
                return Result(json.dumps(item, ensure_ascii=False), {"prompt_tokens": 10,
                                                                     "completion_tokens": 20},
                              provider=self.name, model=self.model)
            text = str(item)
        else:
            text = "رد تجريبي من واجهة الوكيل."
        if json_mode:
            text = json.dumps({"thought": "", "action": None, "final": text}, ensure_ascii=False)
        return Result(text, {"prompt_tokens": 10, "completion_tokens": 20},
                      provider=self.name, model=self.model)


def _iter_sse(response):
    """Yield `data:` payloads from an SSE stream, tolerant of chunk boundaries."""
    buffer = b""
    while True:
        try:
            block = response.readline()
        except Exception:
            return
        if not block:
            return
        buffer += block
        if block in (b"\n", b"\r\n"):
            payload = b"\n".join(line[5:].strip() for line in buffer.split(b"\n")
                                 if line.startswith(b"data:"))
            buffer = b""
            if payload.strip() and payload.strip() != b"[DONE]":
                yield payload.decode("utf-8", "replace")
