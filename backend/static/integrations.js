/* Owner integrations page — logic half.
 *
 * Same rule as docs/assets/components.js, and for the same reason: a card is never
 * drawn green because the page loaded. Every state below is derived from a payload
 * the server actually returned (`/api/owner/integrations` or one of the two list
 * endpoints), and anything not read is reported as «غير معروف» with no colour that
 * implies work happened.
 *
 * This file touches no DOM, so tests/browser_integrations.test.mjs can drive it in
 * node with plain objects. The rendering half lives in integrations-ui.js.
 */
(function (target) {
  'use strict';

  const READY = 'READY';
  const MISSING = 'MISSING';
  const UNKNOWN = 'UNKNOWN';
  const OK = 'OK';
  const PENDING = 'PENDING';
  const BAD = 'BAD';

  const STATE_LABEL = {
    READY: 'جاهز',
    MISSING: 'غير مُهيّأ',
    UNKNOWN: 'غير معروف',
    OK: 'نجح',
    PENDING: 'قيد التنفيذ',
    BAD: 'فشل'
  };

  /* The eight cards the page draws, in the order they appear. `capability` is the
   * key inside the payload's `capabilities` object — the page cannot invent one. */
  const CARDS = [
    {id: 'github-runs', provider: 'github', capability: 'list_runs',
     title: 'GitHub Actions · سجل التشغيلات', method: 'GET',
     endpoint: '/api/owner/integrations/github/runs'},
    {id: 'github-dispatch', provider: 'github', capability: 'dispatch',
     title: 'GitHub Actions · تشغيل workflow', method: 'POST',
     endpoint: '/api/owner/integrations/github/dispatch', mutation: 'github_dispatch'},
    {id: 'vercel-deployments', provider: 'vercel', capability: 'list_deployments',
     title: 'Vercel · سجل النشرات', method: 'GET',
     endpoint: '/api/owner/integrations/vercel/deployments'},
    {id: 'vercel-hook', provider: 'vercel', capability: 'trigger_hook',
     title: 'Vercel · Deploy Hook', method: 'POST',
     endpoint: '/api/owner/integrations/vercel/deploy-hook', mutation: 'vercel_deploy'},
    {id: 'render-deploys', provider: 'render', capability: 'list_deploys',
     title: 'Render · سجل النشرات', method: 'GET',
     endpoint: '/api/owner/integrations/render/deploys'},
    {id: 'render-hook', provider: 'render', capability: 'trigger_hook',
     title: 'Render · Deploy Hook', method: 'POST',
     endpoint: '/api/owner/integrations/render/deploy-hook', mutation: 'render_deploy'},
    {id: 'drive-files', provider: 'drive', capability: 'list_files',
     title: 'Google Drive · ملفات المجلد', method: 'GET',
     endpoint: '/api/owner/integrations/drive/files'},
    {id: 'drive-upload', provider: 'drive', capability: 'upload',
     title: 'Google Drive · رفع ملف نصي', method: 'POST',
     endpoint: '/api/owner/integrations/drive/upload', mutation: 'drive_upload'}
  ];

  /* A capability is READY only when the payload said so. A payload that never
   * mentioned it is UNKNOWN, not MISSING — "the server didn't tell us" and "the
   * server told us no" are different facts and must not share a colour. */
  function capabilityState(entry, capability) {
    if (!entry || typeof entry !== 'object') return UNKNOWN;
    const caps = entry.capabilities;
    if (!caps || typeof caps !== 'object') return UNKNOWN;
    if (!Object.prototype.hasOwnProperty.call(caps, capability)) return UNKNOWN;
    return caps[capability] ? READY : MISSING;
  }

  function missingVariables(entry) {
    if (!entry || !Array.isArray(entry.missing)) return [];
    return entry.missing.filter(function (name) { return typeof name === 'string' && name; });
  }

  function buildCards(status) {
    if (!status || typeof status !== 'object') {
      return CARDS.map(function (card) {
        return {id: card.id, title: card.title, provider: card.provider,
                state: UNKNOWN, label: STATE_LABEL[UNKNOWN], missing: [],
                endpoint: card.endpoint, method: card.method, mutation: card.mutation || null};
      });
    }
    return CARDS.map(function (card) {
      const entry = status[card.provider];
      const state = capabilityState(entry, card.capability);
      return {
        id: card.id,
        title: card.title,
        provider: card.provider,
        state: state,
        label: STATE_LABEL[state],
        missing: missingVariables(entry),
        endpoint: card.endpoint,
        method: card.method,
        mutation: card.mutation || null,
        fingerprint: (entry && entry.token_fingerprint) || ''
      };
    });
  }

  /* GitHub: the conclusion decides once the run is finished; until then the
   * status does. An absent both is UNKNOWN — never a neutral "ok". */
  function runTone(run) {
    if (!run || typeof run !== 'object') return UNKNOWN;
    const conclusion = run.conclusion;
    if (conclusion === 'success') return OK;
    if (conclusion === 'failure' || conclusion === 'timed_out'
        || conclusion === 'action_required') return BAD;
    if (conclusion === 'cancelled' || conclusion === 'skipped') return UNKNOWN;
    if (conclusion) return UNKNOWN;
    const status = run.status;
    if (status === 'in_progress' || status === 'queued' || status === 'waiting'
        || status === 'requested' || status === 'pending') return PENDING;
    return UNKNOWN;
  }

  function runRow(run) {
    const tone = runTone(run);
    const parts = [];
    if (run && run.branch) parts.push(run.branch);
    if (run && run.sha) parts.push(run.sha);
    if (run && run.name) parts.push(run.name);
    return {
      id: (run && run.id) || null,
      title: (run && (run.title || run.name)) || 'تشغيل بلا عنوان',
      meta: parts.join(' · '),
      state: (run && run.status) || null,
      conclusion: (run && run.conclusion) || null,
      tone: tone,
      toneLabel: STATE_LABEL[tone],
      url: (run && run.url) || null
    };
  }

  function deploymentTone(dep) {
    if (!dep || typeof dep !== 'object') return UNKNOWN;
    const state = String(dep.state || '').toUpperCase();
    if (!state) return UNKNOWN;
    if (state === 'READY') return OK;
    if (state === 'ERROR' || state === 'CANCELED') return BAD;
    if (state === 'QUEUED' || state === 'BUILDING' || state === 'INITIALIZING') return PENDING;
    return UNKNOWN;
  }

  function deploymentRow(dep) {
    const tone = deploymentTone(dep);
    const parts = [];
    if (dep && dep.target) parts.push(dep.target);
    if (dep && dep.branch) parts.push(dep.branch);
    if (dep && dep.sha) parts.push(dep.sha);
    return {
      id: (dep && dep.id) || null,
      title: (dep && (dep.name || dep.url)) || 'نشرة بلا اسم',
      meta: parts.join(' · '),
      state: (dep && dep.state) || null,
      created_at: (dep && dep.created_at) || null,
      tone: tone,
      toneLabel: STATE_LABEL[tone],
      url: (dep && dep.url) ? 'https://' + dep.url : null
    };
  }

  /* Render answers its own state words. The mapping is the page's, not the
   * server's: an unlisted word is UNKNOWN — "we have not seen this state", which
   * is not the same claim as "it is fine". */
  function renderTone(deploy) {
    if (!deploy || typeof deploy !== 'object') return UNKNOWN;
    const state = String(deploy.state || '').toLowerCase();
    if (!state) return UNKNOWN;
    if (state === 'live') return OK;
    if (state === 'build_failed' || state === 'canceled' || state === 'timed_out') return BAD;
    if (state === 'created' || state === 'queued' || state === 'build_in_progress'
        || state === 'update_in_progress') return PENDING;
    return UNKNOWN;
  }

  function renderRow(deploy) {
    const tone = renderTone(deploy);
    const parts = [];
    if (deploy && deploy.branch) parts.push(deploy.branch);
    if (deploy && deploy.sha) parts.push(deploy.sha);
    return {
      id: (deploy && deploy.id) || null,
      title: (deploy && (deploy.title || deploy.id)) || 'نشرة بلا اسم',
      meta: parts.join(' · '),
      state: (deploy && deploy.state) || null,
      created_at: (deploy && deploy.created_at) || null,
      tone: tone,
      toneLabel: STATE_LABEL[tone],
      url: (deploy && deploy.url) || null
    };
  }

  /* A Drive listing has no success or failure to grade — a file is just there. So
   * the row carries no tone at all: colouring every row green because the folder
   * answered would be the same lie this page was built to refuse. */
  function driveRow(file) {
    const size = file && typeof file.size === 'number' ? file.size : null;
    return {
      id: (file && file.id) || null,
      name: (file && file.name) || 'بلا اسم',
      mime_type: (file && file.mime_type) || null,
      modified_at: (file && file.modified_at) || null,
      size_label: size === null ? '—' : (size < 1024 ? size + ' B'
        : size < 1048576 ? (size / 1024).toFixed(1) + ' KB'
        : (size / 1048576).toFixed(1) + ' MB'),
      url: (file && file.url) || null
    };
  }

  const ROW_SHAPERS = {run: runRow, deployment: deploymentRow, render: renderRow,
                       drive: driveRow};

  function rowsFrom(payload, kind) {
    if (!payload || typeof payload !== 'object' || !Array.isArray(payload.items)) return [];
    const shape = Object.prototype.hasOwnProperty.call(ROW_SHAPERS, kind)
      ? ROW_SHAPERS[kind] : runRow;
    return payload.items.map(shape);
  }

  /* The confirmation phrase is sent by the server, never hardcoded here: a client
   * that guessed it would still be refused, and a client that cached a stale one
   * should fail loudly rather than silently. */
  function confirmPhraseFor(status, mutation) {
    if (!status || !status.confirm_phrases || !mutation) return '';
    return status.confirm_phrases[mutation] || '';
  }

  const api = {
    CARDS: CARDS,
    STATE_LABEL: STATE_LABEL,
    READY: READY, MISSING: MISSING, UNKNOWN: UNKNOWN, OK: OK, PENDING: PENDING, BAD: BAD,
    buildCards: buildCards,
    capabilityState: capabilityState,
    missingVariables: missingVariables,
    runTone: runTone,
    runRow: runRow,
    deploymentTone: deploymentTone,
    deploymentRow: deploymentRow,
    renderTone: renderTone,
    renderRow: renderRow,
    driveRow: driveRow,
    rowsFrom: rowsFrom,
    confirmPhraseFor: confirmPhraseFor
  };

  if (typeof window !== 'undefined') window.WahaIntegrations = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (typeof globalThis !== 'undefined') {
    globalThis.WahaIntegrations = globalThis.WahaIntegrations || api;
  }
})(typeof window !== 'undefined' ? window : globalThis);
