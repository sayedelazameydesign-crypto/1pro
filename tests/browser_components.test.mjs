/* System components contract, executed in node with plain objects (no DOM, no server).
 *
 * The half no Python test can prove: a card is only green when a payload said so, and a
 * page with no server says "unknown" in words instead of drawing six green dots. If this
 * file ever passes while `components.js` invents a status, the panel stops being a status
 * report and becomes decoration -- which is the failure mode it was written to prevent.
 *
 * Run: node tests/browser_components.test.mjs
 */
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';

const require = createRequire(import.meta.url);
const components = require('../docs/assets/components.js');

let checks = 0;
function test(name, fn) {
  return Promise.resolve().then(fn).then(() => {
    checks += 1;
    console.log('ok - ' + name);
  }).catch(error => {
    console.error('FAIL - ' + name);
    console.error('  ' + (error && error.message ? error.message : error));
    process.exitCode = 1;
  });
}

const cases = [];
function scenario(name, fn) { cases.push(test(name, fn)); }

function states(envelope) {
  return envelope.cards.map(card => card.state);
}
function byKey(envelope, key) {
  return envelope.cards.filter(card => card.key === key)[0];
}

// A payload shaped exactly like the real `/health` (backend/app.py) and
// `/api/agent/config`, so the test cannot pass against an invented contract.
const health = {
  ok: true,
  database: 'postgres',
  ai: 'gemini',
  rag: {ready: true, mode: 'lexical', chunks: 18, indexed_chunks: 18, vocab: 196,
        content_status: 'sample', vector_gate: 'lexical-only', reason: null}
};
const agentConfig = {
  enabled: true,
  ai_mode: 'gemini',
  demo: false,
  model: 'gemini-3.1-flash-lite',
  inline: false,
  execution: {
    mode: 'queued',
    approvals_allowed: true,
    uses_request_visitor_token: false,
    budget: {steps: 5, ai_calls: 8, tool_calls: 8, deadline_seconds: 180},
    caps_applied: {},
    reasons: [],
    new_knobs: 0
  },
  tools: [
    {name: 'calculator', requires_approval: false, read_only: true, network: false},
    {name: 'kb_search', requires_approval: false, read_only: true, network: false},
    {name: 'web_fetch', requires_approval: true, read_only: true, network: true}
  ]
};

scenario('no api_base never claims a component is ready', async () => {
  const out = components.build({apiBase: ''});
  assert.equal(out.mode, 'OFFLINE');
  assert.equal(out.counts.ready, 0);
  states(out).forEach(state => assert.equal(state, 'unknown'));
  assert.equal(out.cards.length, 6, 'the six real components are still named, not hidden');
  assert.match(out.note, /لا api_base|نسخة Pages ثابتة/);
});

scenario('a card without a payload is unknown, never active-and-assumed', async () => {
  const out = components.build({apiBase: 'https://api.test'});
  assert.equal(out.mode, 'UNREACHABLE');
  assert.equal(out.counts.ready, 0);
  states(out).forEach(state => assert.equal(state, 'unknown'));
  assert.match(out.note, /تنام بعد خمول|تعذّر الوصول/);
});

scenario('while the first answer is in flight nothing is drawn green', async () => {
  const out = components.build({apiBase: 'https://api.test', pending: true});
  assert.equal(out.mode, 'LOADING');
  assert.equal(out.counts.ready, 0);
  assert.ok(states(out).every(state => state === 'unknown'));
});

scenario('a live server reports the values it actually sent', async () => {
  const out = components.build({apiBase: 'https://api.test', health, agentConfig, ragMode: 'RAG_LOCAL'});
  assert.equal(out.mode, 'LIVE');
  assert.equal(out.counts.ready, 6);
  assert.ok(byKey(out, 'model').detail.includes('gemini-3.1-flash-lite'),
            'the model name comes from the payload, not from the page');
  assert.ok(byKey(out, 'database').detail.includes('Postgres'));
  assert.ok(byKey(out, 'retrieval').detail.includes('18'), 'the chunk count is the real one');
  assert.match(byKey(out, 'retrieval').detail, /lexical-only/);
});

scenario('AGENT_FAKE is a warning, not a working model', async () => {
  const out = components.build({apiBase: 'https://api.test', health: {...health, ai: 'gemini'},
                                agentConfig: {...agentConfig, demo: true, ai_mode: 'demo'}});
  const model = byKey(out, 'model');
  assert.equal(model.state, 'warn');
  assert.match(model.detail, /AGENT_FAKE|وضع العرض/);
});

scenario('disabled AI says which variable is missing', async () => {
  const out = components.build({apiBase: 'https://api.test',
                                health: {...health, ai: 'disabled'},
                                agentConfig: {...agentConfig, enabled: false, ai_mode: 'disabled', model: null}});
  const model = byKey(out, 'model');
  assert.equal(model.state, 'off');
  assert.match(model.detail, /GEMINI_API_KEY/);
});

scenario('SQLite is called out as unsuitable, not quietly accepted', async () => {
  const out = components.build({apiBase: 'https://api.test', health: {...health, database: 'sqlite'},
                                agentConfig});
  assert.equal(byKey(out, 'database').state, 'warn');
  assert.match(byKey(out, 'database').detail, /تؤقت|تجربة/);
});

scenario('a missing index is an outage with a reason and no invented count', async () => {
  const out = components.build({apiBase: 'https://api.test',
                                health: {...health, rag: {ready: false, mode: null,
                                                         reason: 'الفهرس غير موجود', search_version: 'r2'}},
                                agentConfig});
  const rag = byKey(out, 'retrieval');
  assert.equal(rag.state, 'off');
  assert.match(rag.detail, /الفهرس غير موجود/);
  assert.match(rag.detail, /503/);
  assert.ok(!/\d+\s*مقطع/.test(rag.detail), 'an unreachable index must not borrow a chunk count');
});

scenario('inline execution reports its real budget and the approvals it refuses', async () => {
  const out = components.build({apiBase: 'https://vercel.test', health, agentConfig: {
    ...agentConfig,
    inline: true,
    execution: {mode: 'inline', approvals_allowed: false, uses_request_visitor_token: false,
                budget: {steps: 1, ai_calls: 3, tool_calls: 4, deadline_seconds: 45},
                caps_applied: {MAX_STEPS: 1, DEADLINE_SECONDS: 45}, reasons: [], new_knobs: 0}
  }});
  const execution = byKey(out, 'execution');
  assert.equal(execution.state, 'ready');
  assert.equal(execution.inline, true);
  assert.match(execution.detail, /inline/);
  assert.match(execution.detail, /1 خطوات|خطوات/);
  assert.match(execution.detail, /موافقة/);
});

scenario('the tool card lists what is enabled and which calls need a human', async () => {
  const out = components.build({apiBase: 'https://api.test', health, agentConfig});
  const tools = byKey(out, 'tools');
  assert.equal(tools.state, 'ready');
  assert.deepEqual(tools.names, ['calculator', 'kb_search', 'web_fetch']);
  assert.ok(tools.detail.includes('web_fetch'));
  assert.ok(tools.approval.includes('web_fetch'));
});

scenario('browser filtering is never presented as retrieval', async () => {
  const out = components.build({apiBase: 'https://api.test', health, agentConfig,
                                ragMode: 'BROWSER_FALLBACK'});
  const source = byKey(out, 'source');
  assert.equal(source.state, 'warn');
  assert.notEqual(source.state, 'ready');
  assert.match(source.detail, /ليست استرجاع/);
});

scenario('an untouched search source is unknown, not "turned off"', async () => {
  const out = components.build({apiBase: 'https://api.test', health, agentConfig, ragMode: 'NONE'});
  const source = byKey(out, 'source');
  assert.equal(source.state, 'unknown',
    'NONE means "no retrieval yet", and the page cannot know why -- so it cannot say off');
  assert.match(source.detail, /لم يُطلب بحث|لم يطلب/);
});

scenario('a partial read keeps the unread cards unknown and names the endpoint', async () => {
  const out = components.build({apiBase: 'https://api.test', health: health, agentConfig: null,
                                ragMode: 'RAG_LOCAL'});
  assert.equal(out.mode, 'LIVE');
  assert.equal(byKey(out, 'model').state, 'ready');
  assert.equal(byKey(out, 'execution').state, 'unknown');
  assert.equal(byKey(out, 'tools').state, 'unknown');
  assert.match(out.note, /\/api\/agent\/config/);
});

scenario('counts always match the cards they summarise', async () => {
  const samples = [
    components.build({apiBase: ''}),
    components.build({apiBase: 'https://api.test', pending: true}),
    components.build({apiBase: 'https://api.test', health, agentConfig, ragMode: 'RAG_LOCAL'})
  ];
  samples.forEach(out => {
    const sum = Object.values(out.counts).reduce((total, n) => total + n, 0);
    assert.equal(sum, out.cards.length);
  });
});

scenario('no ready card survives when its own payload is missing', async () => {
  const pairs = [
    ['database', components.build({apiBase: 'https://api.test', health: null, agentConfig})],
    ['retrieval', components.build({apiBase: 'https://api.test', health: null, agentConfig})],
    ['execution', components.build({apiBase: 'https://api.test', health})],
    ['tools', components.build({apiBase: 'https://api.test', health})],
    ['source', components.build({apiBase: 'https://api.test', health, agentConfig})]
  ];
  pairs.forEach(([key, out]) => {
    assert.notEqual(byKey(out, key).state, 'ready', key + ' went green without its payload');
  });
});

Promise.all(cases).then(() => {
  if (!process.exitCode) {
    console.log('system components contract: ok (' + checks + ' checks)');
  }
});
