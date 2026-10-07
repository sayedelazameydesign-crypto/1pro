/* Owner integrations page contract, executed in node with plain objects.
 *
 * The half no Python test can prove: the console draws a capability green only
 * when the server said so, and a page with no answer says «غير معروف» in words
 * instead of colouring four cards. Same failure mode the components panel was
 * written to prevent, on a page that can start a production deployment -- here a
 * card that lies about "ready" invites a mutation that will fail.
 *
 * Run: node tests/browser_integrations.test.mjs
 */
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';

const require = createRequire(import.meta.url);
const logic = require('../backend/static/integrations.js');

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

const fullStatus = {
  github: {configured: true, repo: 'acme/widgets', missing: [], token_fingerprint: 'ab12…cd34',
           capabilities: {list_runs: true, dispatch: true}},
  vercel: {configured: true, project_id: 'prj_1', has_deploy_hook: true, missing: [],
           token_fingerprint: 'ef56…gh78',
           capabilities: {list_deployments: true, trigger_hook: true}},
  confirm_phrases: {github_dispatch: 'dispatch-ci', vercel_deploy: 'deploy'}
};

const byId = (cards, id) => cards.filter(card => card.id === id)[0];

scenario('a page with no payload claims nothing', async () => {
  const cards = logic.buildCards(null);
  assert.equal(cards.length, 4);
  cards.forEach(card => {
    assert.equal(card.state, logic.UNKNOWN, card.id + ' went green with no payload');
    assert.equal(card.label, 'غير معروف');
  });
});

scenario('a configured server makes all four cards ready', async () => {
  const cards = logic.buildCards(fullStatus);
  cards.forEach(card => assert.equal(card.state, logic.READY, card.id));
});

scenario('a capability the server reported false is missing, not unknown', async () => {
  const status = JSON.parse(JSON.stringify(fullStatus));
  status.github.capabilities.dispatch = false;
  status.github.missing = ['GITHUB_WORKFLOW_ID'];
  const card = byId(logic.buildCards(status), 'github-dispatch');
  assert.equal(card.state, logic.MISSING);
  assert.equal(card.label, 'غير مُهيّأ');
  assert.deepEqual(card.missing, ['GITHUB_WORKFLOW_ID']);
});

scenario('a capability the server never mentioned is unknown, not missing', async () => {
  // "the server did not say" and "the server said no" are different facts and must
  // not share a colour -- otherwise a payload shape change reads as a config error.
  const status = JSON.parse(JSON.stringify(fullStatus));
  delete status.vercel.capabilities.trigger_hook;
  const card = byId(logic.buildCards(status), 'vercel-hook');
  assert.equal(card.state, logic.UNKNOWN);
  assert.notEqual(card.state, logic.MISSING);
});

scenario('an absent provider block is unknown, not missing', async () => {
  const card = byId(logic.buildCards({github: fullStatus.github}), 'vercel-hook');
  assert.equal(card.state, logic.UNKNOWN);
  assert.deepEqual(card.missing, []);
});

scenario('a finished successful run reads as ok', async () => {
  assert.equal(logic.runTone({status: 'completed', conclusion: 'success'}), logic.OK);
});

scenario('a failed run reads as bad', async () => {
  assert.equal(logic.runTone({status: 'completed', conclusion: 'failure'}), logic.BAD);
  assert.equal(logic.runTone({status: 'completed', conclusion: 'timed_out'}), logic.BAD);
});

scenario('an unfinished run reads as pending, whatever its status says', async () => {
  assert.equal(logic.runTone({status: 'in_progress'}), logic.PENDING);
  assert.equal(logic.runTone({status: 'queued'}), logic.PENDING);
});

scenario('a run with neither conclusion nor status is unknown', async () => {
  assert.equal(logic.runTone({}), logic.UNKNOWN);
  assert.equal(logic.runTone(null), logic.UNKNOWN);
});

scenario('a run row truncates the sha and keeps no author data', async () => {
  const row = logic.runRow({id: 1, title: 'fix: x', name: 'CI', head_branch: 'main',
                            branch: 'main', sha: 'a'.repeat(40), url: 'https://github.com/x'});
  assert.equal(row.tone, logic.UNKNOWN, 'a row built without a conclusion is unknown');
  const dumped = JSON.stringify(row);
  assert.ok(dumped.length < 400, 'the row kept more than the UI shows');
});

scenario('a run row uses the sha the server already truncated', async () => {
  const row = logic.runRow({id: 1, sha: 'abc123def456', conclusion: 'success',
                            status: 'completed'});
  assert.equal(row.sha, undefined);
  assert.equal(row.tone, logic.OK);
});

scenario('deployment states map to the four tones and nothing else', async () => {
  assert.equal(logic.deploymentTone({state: 'READY'}), logic.OK);
  assert.equal(logic.deploymentTone({state: 'ERROR'}), logic.BAD);
  assert.equal(logic.deploymentTone({state: 'CANCELED'}), logic.BAD);
  assert.equal(logic.deploymentTone({state: 'BUILDING'}), logic.PENDING);
  assert.equal(logic.deploymentTone({state: 'SOMETHING_NEW'}), logic.UNKNOWN);
  assert.equal(logic.deploymentTone({}), logic.UNKNOWN);
});

scenario('a deployment url is made absolute before it reaches an href', async () => {
  const row = logic.deploymentRow({id: 'dpl_1', name: 'widgets',
                                   url: 'widgets-x.vercel.app', state: 'READY',
                                   target: 'production'});
  assert.equal(row.url, 'https://widgets-x.vercel.app');
  assert.equal(row.tone, logic.OK);
});

scenario('rowsFrom never throws on a payload with no items', async () => {
  assert.deepEqual(logic.rowsFrom(null, 'run'), []);
  assert.deepEqual(logic.rowsFrom({}, 'run'), []);
  assert.deepEqual(logic.rowsFrom({items: 'not-a-list'}, 'run'), []);
  assert.deepEqual(logic.rowsFrom({items: []}, 'deployment'), []);
});

scenario('rowsFrom picks the right shaper per kind', async () => {
  const runs = logic.rowsFrom({items: [{id: 1, conclusion: 'success', status: 'completed'}]},
                              'run');
  const deps = logic.rowsFrom({items: [{id: 'd', state: 'READY'}]}, 'deployment');
  assert.equal(runs[0].conclusion, 'success');
  assert.equal(deps[0].created_at === undefined || deps[0].created_at === null, true);
  assert.equal(deps[0].tone, logic.OK);
});

scenario('the confirm phrase comes from the server, never from this file', async () => {
  assert.equal(logic.confirmPhraseFor(fullStatus, 'github_dispatch'), 'dispatch-ci');
  assert.equal(logic.confirmPhraseFor(fullStatus, 'vercel_deploy'), 'deploy');
  const rotated = JSON.parse(JSON.stringify(fullStatus));
  rotated.confirm_phrases.vercel_deploy = 'ship-it';
  assert.equal(logic.confirmPhraseFor(rotated, 'vercel_deploy'), 'ship-it',
               'a rotated phrase on the server was not followed');
});

scenario('a missing payload yields an empty phrase, so the mutation cannot be sent', async () => {
  assert.equal(logic.confirmPhraseFor(null, 'vercel_deploy'), '');
  assert.equal(logic.confirmPhraseFor(fullStatus, 'unknown_action'), '');
  assert.equal(logic.confirmPhraseFor(fullStatus, ''), '');
});

scenario('every card keeps the endpoint it will call', async () => {
  logic.buildCards(fullStatus).forEach(card => {
    assert.ok(card.endpoint.startsWith('/api/owner/integrations/'), card.id);
    assert.ok(card.method === 'GET' || card.method === 'POST', card.id);
    if (card.method === 'POST') {
      assert.ok(card.mutation, card.id + ' is a mutation without a confirm action');
    }
  });
});

Promise.all(cases).then(() => {
  if (!process.exitCode) {
    console.log('owner integrations contract: ok (' + checks + ' checks)');
  }
});
