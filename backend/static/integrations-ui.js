/* Owner integrations console — rendering half.
 *
 * All the deciding lives in integrations.js (DOM-free, tested in node). This file
 * only: holds the session for this tab, calls the API, and paints what came back.
 *
 * Two rules worth stating because they are easy to break later:
 *  - The owner token lives in sessionStorage, not localStorage: one tab, gone on
 *    close. A deploy-capable credential has no business surviving a browser restart.
 *  - Nothing here ever prints a request or response body verbatim into the page.
 *    The server already redacts its own secrets, and echoing raw upstream JSON
 *    would be the one place a leaked hook URL could still surface.
 */
(function () {
  'use strict';

  const logic = window.WahaIntegrations;
  const $ = function (id) { return document.getElementById(id); };

  const state = {token: sessionStorage.getItem('wahaOwnerToken') || '',
                 csrf: sessionStorage.getItem('wahaOwnerCsrf') || '',
                 status: null};

  function saveSession() {
    if (state.token) sessionStorage.setItem('wahaOwnerToken', state.token);
    else sessionStorage.removeItem('wahaOwnerToken');
    if (state.csrf) sessionStorage.setItem('wahaOwnerCsrf', state.csrf);
    else sessionStorage.removeItem('wahaOwnerCsrf');
  }

  function call(path, options) {
    const init = options || {};
    const headers = {'Content-Type': 'application/json'};
    if (state.token) headers['Authorization'] = 'Bearer ' + state.token;
    if (state.csrf) headers['X-Waha-CSRF'] = state.csrf;
    return fetch(path, {method: init.method || 'GET', headers: headers,
                        body: init.body ? JSON.stringify(init.body) : undefined,
                        credentials: 'same-origin'})
      .then(function (response) {
        return response.json().catch(function () { return {}; }).then(function (payload) {
          return {ok: response.ok, status: response.status, payload: payload};
        });
      });
  }

  function log(elementId, result) {
    const box = $(elementId);
    box.hidden = false;
    const body = result.payload || {};
    const summary = result.ok
      ? 'تم · ' + result.status
      : 'فشل · ' + result.status + ' · ' + (body.code || 'error') + ' · ' + (body.error || '');
    box.textContent = summary;
  }

  function paintCards() {
    const host = $('cards');
    host.textContent = '';
    logic.buildCards(state.status).forEach(function (card) {
      const node = document.createElement('div');
      node.className = 'card';
      const title = document.createElement('h3');
      title.textContent = card.title;
      const state_el = document.createElement('div');
      state_el.className = 'state';
      state_el.dataset.tone = card.state;
      state_el.textContent = card.label;
      const meta = document.createElement('div');
      meta.className = 'meta';
      const bits = [card.method + ' ' + card.endpoint];
      if (card.state === 'MISSING' && card.missing.length) {
        bits.push('ناقص: ' + card.missing.join('، '));
      }
      if (card.fingerprint) bits.push('الرمز: ' + card.fingerprint);
      meta.textContent = bits.join('\n');
      node.appendChild(title);
      node.appendChild(state_el);
      node.appendChild(meta);
      host.appendChild(node);
    });

    const cards = logic.buildCards(state.status);
    const dispatchReady = cards.filter(function (c) { return c.id === 'github-dispatch'; })[0];
    const hookReady = cards.filter(function (c) { return c.id === 'vercel-hook'; })[0];
    $('dispatch-box').hidden = !(dispatchReady && dispatchReady.state === logic.READY);
    $('hook-box').hidden = !(hookReady && hookReady.state === logic.READY);
    $('dispatch-hint').textContent = dispatchReady && dispatchReady.state === logic.READY
      ? 'يتطلب نص التأكيد: ' + logic.confirmPhraseFor(state.status, 'github_dispatch')
        + ' · الحد 3 عمليات كتابة في الدقيقة.' : '';
    $('hook-hint').textContent = hookReady && hookReady.state === logic.READY
      ? 'يتطلب نص التأكيد: ' + logic.confirmPhraseFor(state.status, 'vercel_deploy')
        + ' · الحد 3 عمليات كتابة في الدقيقة.' : '';
  }

  function table(rows, columns) {
    if (!rows.length) {
      const empty = document.createElement('p');
      empty.className = 'empty';
      empty.textContent = 'لا نتائج، أو لم يُقرأ السجل بعد.';
      return empty;
    }
    const table_el = document.createElement('table');
    const head = document.createElement('thead');
    const headRow = document.createElement('tr');
    columns.forEach(function (column) {
      const th = document.createElement('th');
      th.textContent = column.label;
      headRow.appendChild(th);
    });
    head.appendChild(headRow);
    table_el.appendChild(head);
    const body = document.createElement('tbody');
    rows.forEach(function (row) {
      const tr = document.createElement('tr');
      columns.forEach(function (column) {
        const td = document.createElement('td');
        if (column.key === 'tone') {
          const span = document.createElement('span');
          span.className = 'tone';
          span.dataset.tone = row.tone;
          span.textContent = row.toneLabel;
          td.appendChild(span);
        } else if (column.key === 'url' && row.url) {
          const link = document.createElement('a');
          link.href = row.url;
          link.rel = 'noopener noreferrer';
          link.target = '_blank';
          link.textContent = 'فتح';
          td.appendChild(link);
        } else {
          td.textContent = row[column.key] === null || row[column.key] === undefined
            ? '—' : String(row[column.key]);
        }
        tr.appendChild(td);
      });
      body.appendChild(tr);
    });
    table_el.appendChild(body);
    return table_el;
  }

  function paintRows(hostId, rows, columns) {
    const host = $(hostId);
    host.textContent = '';
    host.appendChild(table(rows, columns));
  }

  function showSignedIn(signedIn) {
    $('auth-panel').hidden = signedIn;
    $('status-panel').hidden = !signedIn;
    $('github-panel').hidden = !signedIn;
    $('vercel-panel').hidden = !signedIn;
  }

  function applyStatus(payload, note) {
    state.status = payload && payload.integrations ? payload.integrations : payload;
    paintCards();
    $('session-note').textContent = note || '';
  }

  function bootstrap() {
    if (!state.token) {
      showSignedIn(false);
      return;
    }
    showSignedIn(true);
    call('/api/owner/session').then(function (result) {
      if (!result.ok) {
        state.token = '';
        state.csrf = '';
        saveSession();
        showSignedIn(false);
        return;
      }
      state.csrf = result.payload.csrf || state.csrf;
      saveSession();
      applyStatus(result.payload, 'الجلسة صالحة · تنتهي بعد '
        + Math.round((result.payload.expires_in || 0) / 60) + ' دقيقة.');
    });
  }

  document.addEventListener('DOMContentLoaded', function () {
    $('login-form').addEventListener('submit', function (event) {
      event.preventDefault();
      const supplied = $('owner-token').value;
      call('/api/owner/login', {method: 'POST', body: {token: supplied}})
        .then(function (result) {
          $('owner-token').value = '';
          if (!result.ok) {
            window.alert('الدخول فشل: ' + (result.payload.code || result.status));
            return;
          }
          state.token = result.payload.token;
          state.csrf = result.payload.csrf;
          saveSession();
          showSignedIn(true);
          applyStatus(result.payload, 'جلسة جديدة · 8 ساعات.');
        });
    });

    $('sign-out').addEventListener('click', function () {
      // Stateless session: the server keeps no revocation list, so this only drops
      // the token from this tab. The copy in memory is gone; the signed token
      // itself stays valid until its 8h TTL, which the UI states rather than hides.
      state.token = '';
      state.csrf = '';
      saveSession();
      showSignedIn(false);
    });

    $('refresh-runs').addEventListener('click', function () {
      call('/api/owner/integrations/github/runs').then(function (result) {
        log('github-log', result);
        if (!result.ok) {
          paintRows('runs-out', [], []);
          return;
        }
        paintRows('runs-out', logic.rowsFrom(result.payload, 'run'), [
          {key: 'title', label: 'التشغيل'},
          {key: 'meta', label: 'تفاصيل'},
          {key: 'tone', label: 'النتيجة'},
          {key: 'url', label: ''}
        ]);
      });
    });

    $('refresh-deployments').addEventListener('click', function () {
      call('/api/owner/integrations/vercel/deployments').then(function (result) {
        log('vercel-log', result);
        if (!result.ok) {
          paintRows('deployments-out', [], []);
          return;
        }
        paintRows('deployments-out', logic.rowsFrom(result.payload, 'deployment'), [
          {key: 'title', label: 'النشرة'},
          {key: 'meta', label: 'تفاصيل'},
          {key: 'tone', label: 'الحالة'},
          {key: 'url', label: ''}
        ]);
      });
    });

    $('dispatch-run').addEventListener('click', function () {
      const phrase = logic.confirmPhraseFor(state.status, 'github_dispatch');
      if ($('dispatch-confirm').value.trim() !== phrase) {
        window.alert('اكتب نص التأكيد كما هو: ' + phrase);
        return;
      }
      call('/api/owner/integrations/github/dispatch', {
        method: 'POST',
        body: {ref: $('dispatch-ref').value.trim() || 'main', confirm: phrase}
      }).then(function (result) {
        log('github-log', result);
        $('dispatch-confirm').value = '';
      });
    });

    $('deploy-now').addEventListener('click', function () {
      const phrase = logic.confirmPhraseFor(state.status, 'vercel_deploy');
      if ($('deploy-confirm').value.trim() !== phrase) {
        window.alert('اكتب نص التأكيد كما هو: ' + phrase);
        return;
      }
      call('/api/owner/integrations/vercel/deploy-hook', {
        method: 'POST', body: {confirm: phrase}
      }).then(function (result) {
        log('vercel-log', result);
        $('deploy-confirm').value = '';
      });
    });

    bootstrap();
  });
})();
