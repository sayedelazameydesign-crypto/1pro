/* System components panel, browser side: it reads state the server already publishes.
 *
 * The rule this file exists to keep: a component is never drawn green because a page was
 * rendered. Every card is derived from a payload the backend actually returned --
 * `/health` (ai, database, rag) and `/api/agent/config` (execution mode, caps, tools) --
 * and anything not known is reported as `unknown`, in words, with no colour that implies
 * work. A card cannot say "active" for a service nobody asked about.
 *
 *   LIVE         api_base is set and the payloads arrived
 *   LOADING      api_base is set, the first answer has not landed yet
 *   OFFLINE      no api_base: nothing was asked, so nothing is claimed
 *   UNREACHABLE  api_base is set but the calls failed (sleeping free instance, CORS, 5xx)
 *
 * Like search.js, this file touches no DOM, so tests/browser_components.test.mjs can drive
 * it in node with plain objects -- no browser, no server, no network.
 */
(function (target) {
  'use strict';

  const LIVE = 'LIVE';
  const LOADING = 'LOADING';
  const OFFLINE = 'OFFLINE';
  const UNREACHABLE = 'UNREACHABLE';

  // Card states. `unknown` is a first-class state, not a fallback that borrows `ready`.
  const READY = 'ready';
  const WARN = 'warn';
  const OFF = 'off';
  const UNKNOWN = 'unknown';

  const STATE_LABEL = {
    ready: 'جاهز',
    warn: 'يحتاج انتباه',
    off: 'غير مُفعَّل',
    unknown: 'غير معروف'
  };

  const NOT_READ = 'لم تُقرأ الحالة';

  function card(key, name, role, state, detail, extra) {
    const out = {key: key, name: name, role: role, state: state,
                 label: STATE_LABEL[state] || STATE_LABEL.unknown, detail: detail};
    if (extra) Object.keys(extra).forEach(k => { out[k] = extra[k]; });
    return out;
  }

  // Every card built before the server has spoken: same names and roles, `unknown` state,
  // and a detail that says so instead of guessing.
  function unreadCards(reason) {
    return [
      card('model', 'نموذج اللغة', 'المحادثة والوكيل', UNKNOWN, reason || NOT_READ),
      card('database', 'قاعدة البيانات', 'الجلسات والمهام', UNKNOWN, reason || NOT_READ),
      card('retrieval', 'فهرس الاسترجاع', 'بحث معجمي فوق data/rag', UNKNOWN, reason || NOT_READ),
      card('execution', 'وضع التنفيذ', 'أين تُنفَّذ مهمة الوكيل', UNKNOWN, reason || NOT_READ),
      card('tools', 'أدوات الوكيل', 'ما يستطيع الوكيل استدعاءه', UNKNOWN, reason || NOT_READ),
      card('source', 'مصدر البحث', 'من أين جاءت النتائج المعروضة', UNKNOWN, reason || NOT_READ)
    ];
  }

  function modelCard(health, config) {
    const mode = config ? config.ai_mode : null;
    const demo = !!(config && config.demo);
    const disabled = mode === 'disabled' || (!config && health && health.ai === 'disabled');
    if (demo) {
      return card('model', 'نموذج اللغة', 'المحادثة والوكيل', WARN,
                  'وضع العرض (AGENT_FAKE=1): ردود ثابتة بلا نموذج ولا شبكة',
                  {raw: 'demo'});
    }
    if (disabled) {
      return card('model', 'نموذج اللغة', 'المحادثة والوكيل', OFF,
                  'غير مُهيَّأ: لا GEMINI_API_KEY على الخادم، فمساحة العمل معطّلة',
                  {raw: 'disabled'});
    }
    if (mode === 'promptql') {
      return card('model', 'نموذج اللغة', 'المحادثة والوكيل', READY,
                  'خلف بوابة PromptQL · ' + ((config && config.model) || 'النموذج المُعلَن'),
                  {raw: 'promptql'});
    }
    if (mode === 'gemini' || (health && health.ai === 'gemini')) {
      return card('model', 'نموذج اللغة', 'المحادثة والوكيل', READY,
                  (config && config.model) || 'Gemini مباشر · جاهز', {raw: 'gemini'});
    }
    return card('model', 'نموذج اللغة', 'المحادثة والوكيل', UNKNOWN, NOT_READ);
  }

  function databaseCard(health) {
    const kind = health ? health.database : null;
    if (kind === 'postgres') {
      return card('database', 'قاعدة البيانات', 'الجلسات والمهام', READY,
                  'Postgres (Neon) — القرص المؤقت لا يُستخدم للحالة', {raw: 'postgres'});
    }
    if (kind === 'sqlite') {
      return card('database', 'قاعدة البيانات', 'الجلسات والمهام', WARN,
                  'SQLite محلي: يصلح للتجربة فقط، ولا يبقى على قرص مؤقت', {raw: 'sqlite'});
    }
    return card('database', 'قاعدة البيانات', 'الجلسات والمهام', UNKNOWN, NOT_READ);
  }

  function retrievalCard(health) {
    const rag = health ? health.rag : null;
    if (!rag) return card('retrieval', 'فهرس الاسترجاع', 'بحث معجمي فوق data/rag', UNKNOWN, NOT_READ);
    if (!rag.ready) {
      return card('retrieval', 'فهرس الاسترجاع', 'بحث معجمي فوق data/rag', OFF,
                  (rag.reason || 'الفهرس غير منشور على هذا الخادم') + ' → 503 rag_index_missing',
                  {raw: false});
    }
    const chunks = typeof rag.chunks === 'number' ? rag.chunks : null;
    return card('retrieval', 'فهرس الاسترجاع', 'بحث معجمي فوق data/rag', READY,
                (rag.mode || 'lexical') + ' · ' + (chunks === null ? 'عدد المقاطع غير معلن' : chunks + ' مقطعاً')
                + ' · بوابة المتجهات: ' + (rag.vector_gate || 'غير معلنة'),
                {raw: true, chunks: chunks});
  }

  function budgetText(budget, caps) {
    if (!budget) return 'الميزانية غير معلنة';
    const parts = [budget.steps + ' خطوات', budget.ai_calls + ' استدعاء نموذج'];
    if (budget.deadline_seconds) parts.push('مهلة ' + budget.deadline_seconds + 'ث');
    const capped = caps && Object.keys(caps).length;
    return parts.join(' · ') + (capped ? ' (مقصوصة بحسب المنصة)' : '');
  }

  function executionCard(config) {
    const execution = config ? config.execution : null;
    if (!execution) {
      return card('execution', 'وضع التنفيذ', 'أين تُنفَّذ مهمة الوكيل', UNKNOWN, NOT_READ);
    }
    const inline = execution.mode === 'inline';
    const approvals = execution.approvals_allowed;
    const detail = (inline ? 'inline: المهمة تنتهي داخل الطلب' : 'queued: عامل في الخلفية')
      + ' · ' + budgetText(execution.budget, execution.caps_applied)
      + (approvals ? '' : ' · الأدوات التي تحتاج موافقة مرفوضة هنا');
    return card('execution', 'وضع التنفيذ', 'أين تُنفَّذ مهمة الوكيل', READY, detail,
                {raw: execution.mode, inline: inline, approvals_allowed: !!approvals});
  }

  function toolsCard(config) {
    const tools = config ? config.tools : null;
    if (!Array.isArray(tools)) {
      return card('tools', 'أدوات الوكيل', 'ما يستطيع الوكيل استدعاءه', UNKNOWN, NOT_READ);
    }
    if (!tools.length) {
      return card('tools', 'أدوات الوكيل', 'ما يستطيع الوكيل استدعاءه', OFF,
                  'لا أداة مُفعَّلة على هذا الخادم', {raw: 0, names: []});
    }
    const names = tools.map(tool => tool.name);
    const gated = tools.filter(tool => tool.requires_approval).map(tool => tool.name);
    const detail = names.join(' · ')
      + (gated.length ? ' — تحتاج موافقة: ' + gated.join(', ') : '');
    return card('tools', 'أدوات الوكيل', 'ما يستطيع الوكيل استدعاءه', READY, detail,
                {raw: names.length, names: names, approval: gated});
  }

  function sourceCard(ragMode) {
    if (ragMode === 'RAG_LOCAL') {
      return card('source', 'مصدر البحث', 'من أين جاءت النتائج المعروضة', READY,
                  'RAG_LOCAL: مقاطع مُسترجَعة من فهرس الخادم، باستشهاد حرفي', {raw: ragMode});
    }
    if (ragMode === 'BROWSER_FALLBACK') {
      return card('source', 'مصدر البحث', 'من أين جاءت النتائج المعروضة', WARN,
                  'BROWSER_FALLBACK: تصفية محلية لنصوص الصفحة — ليست استرجاعاً ولا تحمل استشهاداً',
                  {raw: ragMode});
    }
    if (ragMode === 'NONE') {
      return card('source', 'مصدر البحث', 'من أين جاءت النتائج المعروضة', OFF,
                  'NONE: لا مصدر (لا خادم مربوط، فلا بحث على الخادم)', {raw: ragMode});
    }
    return card('source', 'مصدر البحث', 'من أين جاءت النتائج المعروضة', UNKNOWN, NOT_READ);
  }

  function tally(cards) {
    const counts = {ready: 0, warn: 0, off: 0, unknown: 0};
    cards.forEach(item => { counts[item.state] = (counts[item.state] || 0) + 1; });
    return counts;
  }

  function envelope(mode, cards, note, extra) {
    const out = {mode: mode, cards: cards, note: note || '', counts: tally(cards)};
    if (extra) Object.keys(extra).forEach(key => { out[key] = extra[key]; });
    return out;
  }

  function build(input) {
    const options = input || {};
    const apiBase = typeof options.apiBase === 'string' ? options.apiBase.trim() : '';
    if (!apiBase) {
      const cards = unreadCards('غير متصل بخادم واحة');
      return envelope(OFFLINE, cards,
        'هذه نسخة Pages ثابتة: لا api_base، فلا حالة خادم تُقرأ. البطاقات رمادية عن قصد — '
        + 'الكتالوج والبحث والتنزيل تعمل الآن، ولا شيء منها يلمس شبكة.', {apiBase: ''});
    }
    if (options.pending && !options.health && !options.agentConfig) {
      return envelope(LOADING, unreadCards('جارٍ قراءة الحالة…'),
        'جارٍ قراءة /health و/api/agent/config. لا تُلوَّن بطاقة قبل أن يصل الرد.', {apiBase: apiBase});
    }
    if (!options.health && !options.agentConfig) {
      return envelope(UNREACHABLE, unreadCards('تعذّر الوصول إلى الخادم'),
        options.error || 'تعذّر الوصول إلى خادم واحة؛ الخادم المجاني ينام بعد خمول، '
        + 'وأول طلب بعده قد يستغرق حتى دقيقة. لا تُعرض حالة غير مقروءة كحالة جاهزة.',
        {apiBase: apiBase});
    }
    const health = options.health || null;
    const config = options.agentConfig || null;
    const cards = [
      modelCard(health, config),
      databaseCard(health),
      retrievalCard(health),
      executionCard(config),
      toolsCard(config),
      sourceCard(options.ragMode)
    ];
    const missing = [];
    if (!health) missing.push('/health');
    if (!config) missing.push('/api/agent/config');
    const note = missing.length
      ? 'قُرئ جزئياً: لم يصل ' + missing.join(' و') + '، والبطاقات التي تعتمد عليه تبقى «غير معروف».'
      : 'كل بطاقة أعلاه مشتقة من رد الخادم نفسه، لا من نجاح تحميل الصفحة.';
    return envelope(LIVE, cards, note, {apiBase: apiBase});
  }

  const api = {
    build: build,
    LIVE: LIVE, LOADING: LOADING, OFFLINE: OFFLINE, UNREACHABLE: UNREACHABLE,
    READY: READY, WARN: WARN, OFF: OFF, UNKNOWN: UNKNOWN,
    STATE_LABEL: STATE_LABEL,
    unreadCards: unreadCards
  };
  if (typeof window !== 'undefined') window.WahaComponents = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (typeof globalThis !== 'undefined') globalThis.WahaComponents = globalThis.WahaComponents || api;
})(typeof window !== 'undefined' ? window : globalThis);
