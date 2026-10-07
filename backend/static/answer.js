/* Progressive answers + source cards, browser side (pure: no DOM here).
 *
 * Two jobs, one honesty rule:
 *   slices()  turns a finished answer into cumulative frames for the typewriter
 *             effect. The reply is already saved server-side; this only paces
 *             how it appears, so a dropped frame can never lose content.
 *   cards()   turns a /api/search payload into source-card HTML. Only rows the
 *             server signed as RAG_LOCAL evidence with a verbatim citation may
 *             become cards -- anything else is an empty note or nothing, never
 *             a source the page invented. The cards are labelled "related
 *             passages", not proof: the chat answer comes from the model, and
 *             retrieval only offers nearby index passages for exploration.
 *
 * This file touches no DOM on purpose, which is what lets
 * tests/browser_answer.test.mjs drive it in node. It is mirrored verbatim at
 * backend/static/answer.js because docs/ (Pages) and backend/static/ (Flask)
 * deploy independently; change both copies together.
 */
(function (target) {
  'use strict';
  var RAG_EVIDENCE = 'RAG_LOCAL';
  var SOURCES_MAX = 3;
  var SNIPPET_CHARS = 220;
  var TITLE_CHARS = 90;
  var MAX_SLICES = 400;

  function escapeHtml(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (c) {
      return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c];
    });
  }

  function truncate(value, limit) {
    var text = String(value == null ? '' : value);
    return text.length > limit ? text.slice(0, limit) + '…' : text;
  }

  /* Cumulative frames of `text`: frame[i] is what the bubble shows at tick i.
   * Long answers grow the step so the whole typing stays under MAX_SLICES
   * frames; the last frame is always the full text, byte for byte. */
  function slices(text, charsPerTick) {
    var full = String(text == null ? '' : text);
    if (!full) return [''];
    var step = Math.max(1, Math.floor(Number(charsPerTick) || 14));
    if (Math.ceil(full.length / step) > MAX_SLICES) {
      step = Math.ceil(full.length / MAX_SLICES);
    }
    var frames = [];
    for (var end = step; end < full.length; end += step) {
      frames.push(full.slice(0, end));
    }
    frames.push(full);
    return frames;
  }

  function isCardRow(row) {
    return !!row && row.rag !== false && typeof row.citation === 'string' && row.citation.trim() !== '';
  }

  function cardRow(row) {
    var title = escapeHtml(truncate(row.title || row.skill_id || '', TITLE_CHARS));
    var snippet = escapeHtml(truncate(row.snippet || '', SNIPPET_CHARS));
    var cite = '<code class="source-cite">' + escapeHtml(row.citation) + '</code>';
    var section = row.section
      ? '<span class="source-section">' + escapeHtml(row.section_label || row.section) + '</span>' : '';
    var open = row.skill_id
      ? '<button type="button" class="source-open" data-source-skill="' +
        escapeHtml(row.skill_id) + '">افتح المهارة</button>' : '';
    return '<div class="source-card"><strong>' + title + '</strong><p>' + snippet +
      '</p><div class="source-foot">' + cite + section + open + '</div></div>';
  }

  function emptyNote() {
    return '<p class="source-empty">لا مقاطع مطابقة في الفهرس لهذا السؤال.</p>';
  }

  function unavailableNote() {
    return '<p class="source-empty">فهرس الاسترجاع غير متاح على هذا الخادم.</p>';
  }

  /* {state, html}: 'cards' | 'empty' | 'ignored'.
   * 'ignored' means "render nothing": the payload was not signed RAG_LOCAL
   * evidence, so there is no honest card to show and no honest note either. */
  function cards(payload) {
    if (!payload || payload.evidence !== RAG_EVIDENCE || !Array.isArray(payload.results)) {
      return {state: 'ignored', html: ''};
    }
    var rows = payload.results.filter(isCardRow).slice(0, SOURCES_MAX);
    if (!rows.length) return {state: 'empty', html: emptyNote()};
    return {state: 'cards', html: '<div class="message-sources"><p class="source-head">' +
      'مقاطع ذات صلة من فهرس Waha — للاستكشاف، وليست توثيقاً لردّ النموذج.</p>' +
      '<div class="source-cards">' + rows.map(cardRow).join('') + '</div></div>'};
  }

  var api = {
    slices: slices,
    cards: cards,
    emptyNote: emptyNote,
    unavailableNote: unavailableNote,
    escapeHtml: escapeHtml,
    SOURCES_MAX: SOURCES_MAX,
    MAX_SLICES: MAX_SLICES
  };
  if (typeof window !== 'undefined') window.WahaAnswer = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (typeof globalThis !== 'undefined') globalThis.WahaAnswer = globalThis.WahaAnswer || api;
})(typeof window !== 'undefined' ? window : globalThis);
