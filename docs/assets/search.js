/* R2, browser side: one envelope, three modes, no mixing.
 *
 *   RAG_LOCAL         chunks retrieved by the server from R1's index (citations included)
 *   BROWSER_FALLBACK  the catalogue text the page already has, filtered locally
 *   NONE              neither is available -- an empty answer, never a invented one
 *
 * The two result shapes are never merged: fallback rows carry `citation: null` and
 * `rag: false`, so a caller cannot render a source it did not retrieve. This file
 * touches no DOM on purpose, which is what lets tests/browser_search.test.mjs drive
 * it in node with a fake fetch.
 */
(function (target) {
  'use strict';
  const RAG_MODE = 'RAG_LOCAL';
  const FALLBACK_MODE = 'BROWSER_FALLBACK';
  const NONE_MODE = 'NONE';
  // A sleeping Render instance takes ~50s to wake. Search does not wait for that:
  // it falls back, says so, and the next call hits a warm server.
  const DEFAULT_TIMEOUT_MS = 2500;
  const MAX_ROWS = 20;
  const SNIPPET_CHARS = 300;

  function envelope(mode, results, note, extra) {
    const out = {mode: mode, results: results, note: note || ''};
    if (extra) Object.keys(extra).forEach(key => { out[key] = extra[key]; });
    return out;
  }

  function truncate(value, limit) {
    const text = String(value == null ? '' : value);
    return text.length > limit ? text.slice(0, limit) + '…' : text;
  }

  function fallbackRows(rows) {
    if (!Array.isArray(rows)) return null;
    return rows.slice(0, MAX_ROWS).map(row => ({
      skill_id: (row && (row.id || row.skill_id)) || null,
      title: (row && (row.name || row.title)) || '',
      section: null,
      citation: null,
      snippet: truncate(row && (row.description || row.snippet), SNIPPET_CHARS),
      score: null,
      rag: false
    }));
  }

  function endpoint(apiBase, query, k) {
    const base = String(apiBase).replace(/\/+$/, '');
    return base + '/api/search?q=' + encodeURIComponent(query) + '&k=' + encodeURIComponent(k);
  }

  async function readPayload(response) {
    try {
      return await response.json();
    } catch (error) {
      return null;
    }
  }

  async function search(options) {
    const settings = options || {};
    const query = String(settings.query == null ? '' : settings.query).trim();
    const k = Math.max(1, Math.min(Number(settings.k) || 5, MAX_ROWS));
    const fetchImpl = settings.fetchImpl || (typeof fetch === 'function' ? fetch : null);
    if (!query) return envelope(NONE_MODE, [], 'سؤال فارغ');
    const apiBase = settings.apiBase ? String(settings.apiBase).trim() : '';
    if (apiBase && fetchImpl) {
      let controller = null;
      let timer = null;
      try {
        if (typeof AbortController === 'function') controller = new AbortController();
        const timeoutMs = Number(settings.timeoutMs) || DEFAULT_TIMEOUT_MS;
        timer = setTimeout(() => { if (controller) controller.abort(); }, timeoutMs);
        const response = await fetchImpl(endpoint(apiBase, query, k), {
          method: 'GET',
          headers: {Accept: 'application/json'},
          signal: controller ? controller.signal : undefined
        });
        if (!response || !response.ok) {
          const payload = response ? await readPayload(response) : null;
          const reason = payload && payload.code === 'rag_index_missing'
            ? 'الفهرس غير منشور على الخادم'
            : 'الخادم رفض الطلب';
          return fallback(settings, query, reason, {httpStatus: response ? response.status : 0});
        }
        const payload = await readPayload(response);
        if (!payload || !Array.isArray(payload.results) || payload.evidence !== RAG_MODE) {
          return fallback(settings, query, 'ردّ غير موقّع كمسترجع RAG: تجاهلناه');
        }
        return envelope(RAG_MODE, payload.results.slice(0, k), '', {payload: payload});
      } catch (error) {
        const aborted = error && (error.name === 'AbortError' || /abort/i.test(String(error.message)));
        return fallback(settings, query, aborted ? 'انتهت مهلة الاتصال بالخادم' : 'تعذّر الوصول إلى الخادم');
      } finally {
        if (timer) clearTimeout(timer);
      }
    }
    return fallback(settings, query, apiBase ? 'لا شبكة متاحة في هذه البيئة' : 'لا خادم واحة متصل');
  }

  function fallback(settings, query, reason, extra) {
    if (typeof settings.browserFilter !== 'function') {
      return envelope(NONE_MODE, [], 'لا نتائج: لا استرجاع من الخادم ولا كتالوج في المتصفح', extra);
    }
    let rows;
    try {
      rows = settings.browserFilter(query);
    } catch (error) {
      return envelope(NONE_MODE, [], 'تعذّرت التصفية المحلية: لا نتائج مُختَرَعة', extra);
    }
    const normalized = fallbackRows(rows);
    if (normalized === null) {
      return envelope(NONE_MODE, [], 'التصفية المحلية أعادت بيانات غير متسلسلة', extra);
    }
    return envelope(FALLBACK_MODE, normalized, reason, extra);
  }

  const api = {
    search: search,
    fallbackRows: fallbackRows,
    endpoint: endpoint,
    RAG_MODE: RAG_MODE,
    FALLBACK_MODE: FALLBACK_MODE,
    NONE_MODE: NONE_MODE,
    DEFAULT_TIMEOUT_MS: DEFAULT_TIMEOUT_MS,
    MAX_ROWS: MAX_ROWS
  };
  if (typeof window !== 'undefined') window.WahaRag = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (typeof globalThis !== 'undefined') globalThis.WahaRag = globalThis.WahaRag || api;
})(typeof window !== 'undefined' ? window : globalThis);
