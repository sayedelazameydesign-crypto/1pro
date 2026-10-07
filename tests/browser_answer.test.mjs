/* Progressive answers + source cards contract, executed in node.
 *
 * This is the half no Python test can prove: that the typewriter frames always
 * end at the full answer byte for byte, and that a card can never be built
 * from a row the server did not sign as RAG_LOCAL evidence with a citation.
 * Run: node tests/browser_answer.test.mjs
 */
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';

const require = createRequire(import.meta.url);
const answer = require('../docs/assets/answer.js');

let checks = 0;
const cases = [];
function scenario(name, fn) {
  cases.push(fn().then(() => {
    checks += 1;
    console.log('ok - ' + name);
  }).catch(error => {
    console.error('FAIL - ' + name);
    console.error('  ' + (error && error.message ? error.message : error));
    process.exitCode = 1;
  }));
}

function ragPayload(rows) {
  return {evidence: 'RAG_LOCAL', results: rows, query: 'بريد'};
}

function cardRow(overrides) {
  return Object.assign({
    skill_id: 'SKL001',
    title: 'مهارة البريد',
    section: 'starter',
    section_label: 'البداية',
    citation: 'SKL001#starter#0',
    snippet: 'نص المقطع هنا',
    score: 0.55,
    rag: true
  }, overrides || {});
}

scenario('frames are cumulative and end at the full answer', async () => {
  const frames = answer.slices('abcdefghij', 4);
  assert.deepEqual(frames, ['abcd', 'abcdefgh', 'abcdefghij']);
});

scenario('an empty answer is one empty frame, never zero', async () => {
  assert.deepEqual(answer.slices(''), ['']);
  assert.deepEqual(answer.slices(null), ['']);
});

scenario('long answers grow the step instead of the frame count', async () => {
  const frames = answer.slices('x'.repeat(100000), 1);
  assert.ok(frames.length <= answer.MAX_SLICES);
  assert.equal(frames[frames.length - 1], 'x'.repeat(100000));
});

scenario('a signed RAG payload becomes labelled cards', async () => {
  const out = answer.cards(ragPayload([cardRow(), cardRow({skill_id: 'SKL002', citation: 'SKL002#starter#1'})]));
  assert.equal(out.state, 'cards');
  assert.match(out.html, /مقاطع ذات صلة من فهرس Waha/);
  assert.match(out.html, /ليست توثيقاً لردّ النموذج/);
  assert.match(out.html, /SKL001#starter#0/);
  assert.match(out.html, /data-source-skill="SKL002"/);
});

scenario('cards are capped at SOURCES_MAX', async () => {
  const rows = [1, 2, 3, 4, 5].map(n => cardRow({citation: 'SKL001#starter#' + n}));
  const out = answer.cards(ragPayload(rows));
  assert.equal(out.state, 'cards');
  assert.equal((out.html.match(/class="source-card"/g) || []).length, answer.SOURCES_MAX);
});

scenario('rows without a citation never become cards', async () => {
  const out = answer.cards(ragPayload([cardRow({citation: null}), cardRow({citation: '  '})]));
  assert.equal(out.state, 'empty');
  assert.doesNotMatch(out.html, /source-card/);
});

scenario('browser-fallback rows (rag:false) never become cards', async () => {
  const out = answer.cards(ragPayload([cardRow({rag: false})]));
  assert.equal(out.state, 'empty');
});

scenario('an unsigned payload is ignored, not rendered as sources', async () => {
  for (const payload of [null, {}, {evidence: 'BROWSER_FALLBACK', results: [cardRow()]}, {evidence: 'RAG_LOCAL'}]) {
    const out = answer.cards(payload);
    assert.equal(out.state, 'ignored');
    assert.equal(out.html, '');
  }
});

scenario('zero RAG results is an honest empty note', async () => {
  const out = answer.cards(ragPayload([]));
  assert.equal(out.state, 'empty');
  assert.match(out.html, /لا مقاطع مطابقة في الفهرس/);
});

scenario('card HTML escapes every field', async () => {
  const out = answer.cards(ragPayload([cardRow({
    title: '<img src=x onerror=1>',
    snippet: 'a&b<"c">',
    citation: 'SKL001#starter#0',
    section_label: '</div><script>',
    skill_id: 'SKL001" onmouseover="1'
  })]));
  assert.doesNotMatch(out.html, /<img|<script|onmouseover="/);
  assert.match(out.html, /&lt;img|&amp;|&quot;|&#39;|&lt;\/div&gt;/);
});

scenario('long titles and snippets are truncated, citations pass through verbatim', async () => {
  const out = answer.cards(ragPayload([cardRow({title: 't'.repeat(500), snippet: 's'.repeat(900)})]));
  assert.match(out.html, /…/);
  assert.match(out.html, /SKL001#starter#0/);
});

scenario('the unavailable note names the missing index, nothing else', async () => {
  assert.match(answer.unavailableNote(), /فهرس الاسترجاع غير متاح/);
});

await Promise.all(cases);
if (!process.exitCode) {
  console.log(`browser answer contract: ok (${checks} checks)`);
} else {
  console.error('browser answer contract: FAILED');
}
