/* R2 browser contract, executed in node with a fake fetch.
 *
 * This is the half of R2 that no Python test can prove: that a page without a
 * server still answers honestly, and that a page WITH a server never passes local
 * filtering off as retrieval. Run: node tests/browser_search.test.mjs
 */
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';

const require = createRequire(import.meta.url);
const rag = require('../docs/assets/search.js');

let checks = 0;
function test(name, fn) {
  return fn().then(() => {
    checks += 1;
    console.log('ok - ' + name);
  }).catch(error => {
    console.error('FAIL - ' + name);
    console.error('  ' + (error && error.message ? error.message : error));
    process.exitCode = 1;
  });
}

function jsonResponse(payload, status) {
  return {ok: !status || status < 400, status: status || 200, json: async () => payload};
}

function ragPayload(count) {
  return {
    evidence: 'RAG_LOCAL',
    search_mode: 'lexical',
    results: Array.from({length: count}, (unused, index) => ({
      ci: index,
      chunk_id: 'sha256:' + index,
      citation: 'SKL00' + (index + 1) + '#prompt',
      skill_id: 'SKL00' + (index + 1),
      section: 'prompt',
      title: 'مهارة ' + index,
      snippet: 'نص المقطع ' + index,
      score: 1 / (index + 1),
      rag: true
    }))
  };
}

const catalog = [
  {id: 'SKL003', name: 'إدارة الوقت', description: 'رتّب أولوياتك.', tags: ['وقت']},
  {id: 'SKL005', name: 'البريد المهني', description: 'رسالة موجزة.', tags: ['بريد']}
];

function browserFilter(query) {
  return catalog.filter(row => (row.name + ' ' + row.description).includes(query));
}

const cases = [];
function scenario(name, fn) { cases.push(test(name, fn)); }

scenario('server answer is labelled RAG_LOCAL and passed through untouched', async () => {
  const calls = [];
  const out = await rag.search({
    query: 'متى أستخدم الوسيط', apiBase: 'https://api.test/', k: 5, browserFilter: browserFilter,
    fetchImpl: async (url, options) => {
      calls.push({url, options});
      return jsonResponse(ragPayload(2));
    }
  });
  assert.equal(out.mode, 'RAG_LOCAL');
  assert.equal(out.results.length, 2);
  assert.equal(out.results[0].citation, 'SKL001#prompt', 'citations must survive verbatim');
  assert.equal(out.results[0].rag, true);
  assert.equal(out.payload.search_mode, 'lexical', 'the R1 index metadata is attached');
  assert.equal(calls.length, 1);
  assert.match(calls[0].url, /^https:\/\/api\.test\/api\/search\?q=.*&k=5$/);
  assert.ok(calls[0].url.includes(encodeURIComponent('أستخدم')), 'the query must be encoded');
  assert.equal(calls[0].options.signal.constructor.name, 'AbortSignal');
});

scenario('a same-origin api_base still builds a valid url', async () => {
  let seen = '';
  await rag.search({query: 'بريد', apiBase: '.', fetchImpl: async url => {
    seen = url;
    return jsonResponse(ragPayload(1));
  }});
  assert.equal(seen, './api/search?q=' + encodeURIComponent('بريد') + '&k=5');
});

scenario('503 with rag_index_missing degrades to the labelled browser filter', async () => {
  const out = await rag.search({
    query: 'بريد', apiBase: 'https://api.test', browserFilter: browserFilter,
    fetchImpl: async () => jsonResponse({error: 'no index', code: 'rag_index_missing'}, 503)
  });
  assert.equal(out.mode, 'BROWSER_FALLBACK');
  assert.match(out.note, /الفهرس غير منشور/);
  assert.ok(out.results.length >= 1);
  out.results.forEach(row => {
    assert.equal(row.citation, null, 'a local filter may not present a citation');
    assert.equal(row.rag, false);
    assert.equal(row.score, null, 'no fake relevance score in fallback');
  });
});

scenario('network failure falls back without inventing success', async () => {
  const out = await rag.search({
    query: 'بريد', apiBase: 'https://api.test', browserFilter: browserFilter,
    fetchImpl: async () => { throw new TypeError('Failed to fetch'); }
  });
  assert.equal(out.mode, 'BROWSER_FALLBACK');
  assert.match(out.note, /تعذّر الوصول/);
});

scenario('timeout falls back and does not hang', async () => {
  const started = Date.now();
  const out = await rag.search({
    query: 'بريد', apiBase: 'https://api.test', browserFilter: browserFilter, timeoutMs: 40,
    fetchImpl: (url, options) => new Promise((resolve, reject) => {
      const timer = setTimeout(() => resolve(jsonResponse(ragPayload(3))), 5000);
      if (options.signal) options.signal.addEventListener('abort', () => {
        clearTimeout(timer);
        const error = new Error('aborted');
        error.name = 'AbortError';
        reject(error);
      });
    })
  });
  assert.equal(out.mode, 'BROWSER_FALLBACK');
  assert.match(out.note, /انتهت مهلة/);
  assert.ok(Date.now() - started < 2000, 'the timeout must actually fire');
});

scenario('a payload that is not signed as retrieval is discarded', async () => {
  const out = await rag.search({
    query: 'بريد', apiBase: 'https://api.test', browserFilter: browserFilter,
    fetchImpl: async () => jsonResponse({results: [{citation: 'made-up'}], evidence: 'something'})
  });
  assert.equal(out.mode, 'BROWSER_FALLBACK');
  assert.match(out.note, /غير موقّع/);
  out.results.forEach(row => assert.equal(row.citation, null, 'the untrusted citation is dropped'));
});

scenario('broken JSON is treated as no server', async () => {
  const out = await rag.search({
    query: 'بريد', apiBase: 'https://api.test', browserFilter: browserFilter,
    fetchImpl: async () => ({ok: true, status: 200, json: async () => { throw new Error('not json'); }})
  });
  assert.equal(out.mode, 'BROWSER_FALLBACK');
});

scenario('static Pages mode never calls fetch', async () => {
  let calls = 0;
  const out = await rag.search({
    query: 'بريد', browserFilter: browserFilter,
    fetchImpl: async () => { calls += 1; return jsonResponse(ragPayload(1)); }
  });
  assert.equal(calls, 0, 'no api_base means no network attempt at all');
  assert.equal(out.mode, 'BROWSER_FALLBACK');
  assert.match(out.note, /لا خادم واحة متصل/);
});

scenario('no server and no catalog fails closed', async () => {
  const out = await rag.search({query: 'بريد', fetchImpl: async () => { throw new Error('down'); }});
  assert.equal(out.mode, 'NONE');
  assert.deepEqual(out.results, []);
  assert.ok(out.note.length > 0, 'the reason is stated, not hidden');
});

scenario('an empty query is refused before any work', async () => {
  let calls = 0;
  const out = await rag.search({
    query: '   ', apiBase: 'https://api.test', browserFilter: browserFilter,
    fetchImpl: async () => { calls += 1; return jsonResponse(ragPayload(1)); }
  });
  assert.equal(calls, 0);
  assert.equal(out.mode, 'NONE');
  assert.deepEqual(out.results, []);
});

scenario('a throwing local filter yields NONE, not an empty success', async () => {
  const out = await rag.search({
    query: 'بريد', apiBase: 'https://api.test',
    browserFilter: () => { throw new Error('state not loaded'); },
    fetchImpl: async () => { throw new Error('offline'); }
  });
  assert.equal(out.mode, 'NONE');
  assert.match(out.note, /لا نتائج مُختَرَعة/);
});

scenario('a local filter returning junk yields NONE', async () => {
  const out = await rag.search({query: 'بريد', browserFilter: () => ({not: 'an array'})});
  assert.equal(out.mode, 'NONE');
  assert.match(out.note, /غير متسلسلة/);
});

scenario('modes are never mixed in one envelope', async () => {
  const server = await rag.search({
    query: 'بريد', apiBase: 'https://api.test', browserFilter: browserFilter,
    fetchImpl: async () => jsonResponse(ragPayload(3))
  });
  assert.ok(server.results.every(row => row.rag === true && row.citation));
  const local = await rag.search({query: 'بريد', browserFilter: browserFilter});
  assert.ok(local.results.every(row => row.rag === false && row.citation === null));
  const modes = new Set([rag.RAG_MODE, rag.FALLBACK_MODE, rag.NONE_MODE]);
  assert.equal(modes.size, 3);
  [server, local].forEach(out => assert.ok(modes.has(out.mode), 'undeclared mode: ' + out.mode));
});

scenario('fallback rows are bounded and snippets truncated', async () => {
  const wide = Array.from({length: 60}, (unused, index) => ({
    id: 'SKL' + index, name: 'بريد ' + index, description: 'ل'.repeat(900)
  }));
  const out = await rag.search({query: 'بريد', browserFilter: () => wide});
  assert.equal(out.results.length, rag.MAX_ROWS);
  out.results.forEach(row => assert.ok(row.snippet.length <= 301, 'snippet cap'));
  const trimmed = await rag.search({query: 'لا يوجد', browserFilter: () => []});
  assert.equal(trimmed.mode, 'BROWSER_FALLBACK');
  assert.deepEqual(trimmed.results, [], 'an honest zero-result local filter stays zero');
});

scenario('k is clamped client-side too', async () => {
  let url = '';
  await rag.search({query: 'بريد', apiBase: 'https://api.test', k: 900, fetchImpl: async input => {
    url = input;
    return jsonResponse(ragPayload(5));
  }});
  assert.match(url, /&k=20$/);
});

await Promise.all(cases);
if (!process.exitCode) {
  console.log(`browser search contract: ok (${checks} checks)`);
} else {
  console.error('browser search contract: FAILED');
}
