/* Stránka pro výběr komponent ze nalezených inzerátů.
 *
 * Data (docs/data.json) generuje monitor, stav (docs/state.json) klikáním
 * uživatel. Stav se vždy ukládá do localStorage; když je zadaný GitHub token,
 * commituje se navíc do repozitáře, takže se přenese i na jiné zařízení.
 *
 * POZOR na bezpečnost: titulky a odkazy pocházejí z bazarů, tedy od cizích
 * lidí. Vykresluje se proto výhradně přes textContent a odkazy se propouští
 * jen po kontrole protokolu a domény - jinak by titulek inzerátu mohl
 * propašovat skript, který z localStorage vytáhne token.
 */
'use strict';

var REPO = 'mvasicekm/Sada-pro-zenu';
var STATE_PATH = 'docs/state.json';
var ALLOWED_HOSTS = ['bazos.cz', 'cyklobazar.cz'];
var LS_STATE = 'sada.state.v1';
var LS_TOKEN = 'sada.token.v1';
var SITE_LABELS = { bazos: 'Bazoš', cyklobazar: 'Cyklobazar' };

var data = { items: [], categories: [], total_budget: null };
var state = {};
var stateSha = null;
var pushTimer = null;

/* ---------- pomocné ---------- */

function $(id) { return document.getElementById(id); }

function money(value) {
  if (value === null || value === undefined) { return 'Dohodou'; }
  return String(value).replace(/\B(?=(\d{3})+(?!\d))/g, '\u00a0') + '\u00a0Kč';
}

function registrableHost(hostname) {
  return hostname.toLowerCase().split('.').slice(-2).join('.');
}

/** Vrátí URL jen pokud je to https odkaz na známý bazar, jinak null. */
function safeUrl(raw) {
  var parsed;
  try {
    parsed = new URL(String(raw), location.href);
  } catch (err) {
    return null;
  }
  if (parsed.protocol !== 'https:') { return null; }
  if (ALLOWED_HOSTS.indexOf(registrableHost(parsed.hostname)) === -1) { return null; }
  return parsed.href;
}

function b64encode(text) {
  var bytes = new TextEncoder().encode(text);
  var binary = '';
  for (var i = 0; i < bytes.length; i++) {
    binary += String.fromCharCode(bytes[i]);
  }
  return btoa(binary);
}

function b64decode(text) {
  var binary = atob(String(text).replace(/\s/g, ''));
  var bytes = new Uint8Array(binary.length);
  for (var i = 0; i < binary.length; i++) {
    bytes[i] = binary.charCodeAt(i);
  }
  return new TextDecoder().decode(bytes);
}

/* ---------- stav ---------- */

function readLocalState() {
  try {
    var raw = localStorage.getItem(LS_STATE);
    var parsed = raw ? JSON.parse(raw) : null;
    return parsed && typeof parsed === 'object' ? parsed : {};
  } catch (err) {
    return {};
  }
}

function writeLocalState() {
  try {
    localStorage.setItem(LS_STATE, JSON.stringify(state));
  } catch (err) {
    /* plná nebo zakázaná storage - stav zůstane aspoň do zavření stránky */
  }
}

function getToken() {
  try {
    return localStorage.getItem(LS_TOKEN) || '';
  } catch (err) {
    return '';
  }
}

/** Sloučí dvě sady voleb, u každé položky vyhrává novější časová značka. */
function mergeState(a, b) {
  var out = {};
  [a, b].forEach(function (source) {
    Object.keys(source || {}).forEach(function (uid) {
      var entry = source[uid];
      if (!entry || (entry.flag !== 'like' && entry.flag !== 'skip')) { return; }
      var current = out[uid];
      if (!current || String(entry.at || '') > String(current.at || '')) {
        out[uid] = { flag: entry.flag, at: entry.at || '' };
      }
    });
  });
  return out;
}

function flagOf(uid) {
  return state[uid] ? state[uid].flag : null;
}

function setFlag(uid, flag) {
  if (flagOf(uid) === flag) {
    delete state[uid];
  } else {
    state[uid] = { flag: flag, at: new Date().toISOString() };
  }
  writeLocalState();
  render();
  schedulePush();
}

/* ---------- synchronizace s GitHubem ---------- */

function apiUrl() {
  return 'https://api.github.com/repos/' + REPO + '/contents/' + STATE_PATH;
}

function apiHeaders(token) {
  return {
    'Accept': 'application/vnd.github+json',
    'X-GitHub-Api-Version': '2022-11-28',
    'Authorization': 'Bearer ' + token
  };
}

function parseStatePayload(text) {
  var parsed = JSON.parse(text);
  return parsed && typeof parsed.items === 'object' && parsed.items
    ? parsed.items
    : {};
}

/** Načte vzdálený stav. S tokenem přes API (svěží + dá sha), jinak ze souboru. */
function fetchRemoteState() {
  var token = getToken();
  if (!token) {
    return fetch('state.json?t=' + Date.now(), { cache: 'no-store' })
      .then(function (res) { return res.ok ? res.text() : '{}'; })
      .then(function (text) {
        stateSha = null;
        return parseStatePayload(text);
      })
      .catch(function () { return {}; });
  }
  return fetch(apiUrl(), { headers: apiHeaders(token), cache: 'no-store' })
    .then(function (res) {
      if (res.status === 404) { stateSha = null; return '{}'; }
      if (!res.ok) { throw new Error('GitHub API ' + res.status); }
      return res.json().then(function (body) {
        stateSha = body.sha;
        return b64decode(body.content || '');
      });
    })
    .then(parseStatePayload);
}

function schedulePush() {
  if (!getToken()) { return; }
  if (pushTimer) { clearTimeout(pushTimer); }
  setSyncStatus('Ukládám\u2026');
  pushTimer = setTimeout(function () {
    pushTimer = null;
    pushRemoteState(true);
  }, 1200);
}

function pushRemoteState(retryOnConflict) {
  var token = getToken();
  if (!token) { return Promise.resolve(); }

  var payload = {
    updated_at: new Date().toISOString(),
    items: state
  };
  var body = {
    message: 'chore: update state.json [skip ci]',
    content: b64encode(JSON.stringify(payload, null, 2) + '\n')
  };
  if (stateSha) { body.sha = stateSha; }

  return fetch(apiUrl(), {
    method: 'PUT',
    headers: apiHeaders(token),
    body: JSON.stringify(body)
  }).then(function (res) {
    if (res.status === 409 || res.status === 422) {
      // Soubor se mezitím změnil (jiné zařízení). Stav sloučíme a zkusíme znovu.
      if (!retryOnConflict) { throw new Error('konflikt verzí'); }
      return fetchRemoteState().then(function (remote) {
        state = mergeState(remote, state);
        writeLocalState();
        render();
        return pushRemoteState(false);
      });
    }
    if (!res.ok) { throw new Error('GitHub API ' + res.status); }
    return res.json().then(function (result) {
      stateSha = result.content ? result.content.sha : null;
      setSyncStatus('Uloženo do repozitáře.');
    });
  }).catch(function (err) {
    setSyncStatus('Uložení do repozitáře selhalo: ' + err.message
      + ' (volby máš i tak uložené v prohlížeči)');
  });
}

function setSyncStatus(text) {
  $('token-status').textContent = text;
}

/* ---------- vykreslení ---------- */

function categoryOrder() {
  var order = data.categories.map(function (c) { return c.key; });
  data.items.forEach(function (item) {
    if (order.indexOf(item.category_key) === -1) { order.push(item.category_key); }
  });
  return order;
}

function categoryInfo(key) {
  var found = null;
  data.categories.forEach(function (c) { if (c.key === key) { found = c; } });
  if (found) { return found; }
  var item = null;
  data.items.forEach(function (i) { if (i.category_key === key && !item) { item = i; } });
  return {
    key: key,
    label: item ? (item.category_label || key) : key,
    max_price: item ? item.category_max_price : null
  };
}

function withinLimit(item) {
  if (item.price === null || item.price === undefined) { return false; }
  if (item.category_max_price === null || item.category_max_price === undefined) {
    return true;
  }
  return item.price <= item.category_max_price;
}

function buildCard(item) {
  var flag = flagOf(item.uid);
  var card = document.createElement('article');
  card.className = 'card';
  if (flag === 'like') { card.className += ' card--liked'; }
  if (flag === 'skip') { card.className += ' card--rejected'; }
  if (!withinLimit(item)) { card.className += ' card--over'; }

  var body = document.createElement('div');
  body.className = 'card__body';

  var url = safeUrl(item.url);
  var title = document.createElement(url ? 'a' : 'span');
  title.className = 'card__title';
  title.textContent = item.title || '(bez názvu)';
  if (url) {
    title.href = url;
    title.target = '_blank';
    title.rel = 'noopener noreferrer';
  }
  body.appendChild(title);

  var meta = document.createElement('div');
  meta.className = 'card__meta';
  var bits = [SITE_LABELS[item.site] || item.site];
  if (item.first_seen) { bits.push('nalezeno ' + item.first_seen.slice(0, 10)); }
  if (!withinLimit(item) && item.price !== null && item.price !== undefined) {
    bits.push('nad limit ' + money(item.category_max_price));
  }
  meta.textContent = bits.join(' \u00b7 ');
  body.appendChild(meta);

  var price = document.createElement('div');
  price.className = 'card__price';
  price.textContent = money(item.price);

  var actions = document.createElement('div');
  actions.className = 'card__actions';
  [
    { flag: 'like', glyph: '\u2764\ufe0f', title: 'Líbí se mi' },
    { flag: 'skip', glyph: '\u2716\ufe0f', title: 'Nechci' }
  ].forEach(function (spec) {
    var button = document.createElement('button');
    button.type = 'button';
    button.className = 'icon';
    button.textContent = spec.glyph;
    button.title = spec.title;
    button.setAttribute('aria-label', spec.title);
    button.setAttribute('aria-pressed', flag === spec.flag ? 'true' : 'false');
    button.addEventListener('click', function () { setFlag(item.uid, spec.flag); });
    actions.appendChild(button);
  });

  card.appendChild(body);
  card.appendChild(price);
  card.appendChild(actions);
  return card;
}

function renderList() {
  var showRejected = $('show-rejected').checked;
  var onlyWithin = $('only-within').checked;
  var list = $('list');
  list.textContent = '';

  var shown = 0;
  var hidden = 0;

  categoryOrder().forEach(function (key) {
    var info = categoryInfo(key);
    var items = data.items.filter(function (item) {
      if (item.category_key !== key) { return false; }
      var flag = flagOf(item.uid);
      if (flag === 'skip' && !showRejected) { hidden++; return false; }
      if (onlyWithin && flag !== 'like' && !withinLimit(item)) { hidden++; return false; }
      return true;
    });
    if (!items.length) { return; }
    shown += items.length;

    var group = document.createElement('section');
    group.className = 'group';

    var head = document.createElement('div');
    head.className = 'group__head';
    var pill = document.createElement('span');
    pill.className = 'pill';
    pill.textContent = info.label;
    head.appendChild(pill);
    var note = document.createElement('span');
    note.className = 'muted';
    note.textContent = 'limit ' + money(info.max_price) + ' \u00b7 '
      + items.length + '\u00a0nab.';
    head.appendChild(note);
    group.appendChild(head);

    items.forEach(function (item) { group.appendChild(buildCard(item)); });
    list.appendChild(group);
  });

  if (!shown) {
    var empty = document.createElement('p');
    empty.className = 'muted';
    empty.textContent = 'Nic k zobrazení. Zkus povolit nabídky nad limit'
      + ' nebo zobrazit zamítnuté.';
    list.appendChild(empty);
  }

  $('counts').textContent = 'zobrazeno ' + shown + ' z ' + data.items.length
    + (hidden ? ' (skryto ' + hidden + ')' : '');
}

function renderCalc() {
  var budget = data.total_budget;
  var liked = data.items.filter(function (item) { return flagOf(item.uid) === 'like'; });
  var sum = 0;
  liked.forEach(function (item) { sum += item.price || 0; });

  $('calc-sum').textContent = money(sum);
  $('calc-budget').textContent = budget === null || budget === undefined
    ? '\u2013' : money(budget);

  var total = document.querySelector('.calc__total');
  var bar = $('calc-bar');
  var over = budget ? sum > budget : false;
  total.className = 'calc__total' + (liked.length ? (over ? ' over' : ' ok') : '');
  bar.style.width = budget ? Math.min(100, (sum / budget) * 100) + '%' : '0';
  bar.className = 'bar__fill' + (over ? ' over' : '');

  var list = $('calc-list');
  list.textContent = '';
  var missing = [];

  categoryOrder().forEach(function (key) {
    var info = categoryInfo(key);
    var picked = liked.filter(function (item) { return item.category_key === key; });
    if (!picked.length) {
      missing.push(info.label);
      return;
    }
    var subtotal = 0;
    picked.forEach(function (item) { subtotal += item.price || 0; });

    var row = document.createElement('li');
    var left = document.createElement('span');
    left.textContent = info.label
      + (picked.length > 1 ? ' (' + picked.length + '\u00d7)' : '');
    var right = document.createElement('span');
    right.textContent = money(subtotal);
    row.appendChild(left);
    row.appendChild(right);
    list.appendChild(row);
  });

  var note = $('calc-note');
  if (!liked.length) {
    note.textContent = 'Zaklikej srdíčko u toho, co se ti líbí,'
      + ' a tady se ti to sečte.';
    return;
  }
  var parts = [];
  if (budget) {
    parts.push(over
      ? 'Přetahuješ rozpočet o ' + money(sum - budget) + '.'
      : 'Zbývá ' + money(budget - sum) + '.');
  }
  if (missing.length) {
    parts.push('Chybí: ' + missing.join(', ') + '.');
  } else {
    parts.push('Máš vybráno ze všech kategorií.');
  }
  note.textContent = parts.join(' ');
}

function render() {
  renderCalc();
  renderList();
}

/* ---------- start ---------- */

function renderMeta() {
  var parts = [data.items.length + ' inzerátů'];
  if (data.generated_at) {
    parts.push('aktualizováno ' + data.generated_at.slice(0, 16).replace('T', ' ')
      + ' UTC');
  }
  parts.push(getToken() ? 'synchronizace zapnutá' : 'volby jen v tomto prohlížeči');
  $('meta').textContent = parts.join(' \u00b7 ');
}

function bindControls() {
  $('show-rejected').addEventListener('change', renderList);
  $('only-within').addEventListener('change', renderList);

  $('settings-toggle').addEventListener('click', function () {
    var panel = $('settings');
    panel.hidden = !panel.hidden;
    if (!panel.hidden) { $('token').focus(); }
  });

  $('token-save').addEventListener('click', function () {
    var value = $('token').value.trim();
    if (!value) {
      setSyncStatus('Zadej token, nebo ho nech prázdný a klikni na Zapomenout.');
      return;
    }
    try {
      localStorage.setItem(LS_TOKEN, value);
    } catch (err) {
      setSyncStatus('Prohlížeč nedovolil token uložit.');
      return;
    }
    $('token').value = '';
    setSyncStatus('Zkouším zápis\u2026');
    renderMeta();
    fetchRemoteState().then(function (remote) {
      state = mergeState(remote, state);
      writeLocalState();
      render();
      return pushRemoteState(true);
    }).catch(function (err) {
      setSyncStatus('Token nefunguje: ' + err.message);
    });
  });

  $('token-clear').addEventListener('click', function () {
    try { localStorage.removeItem(LS_TOKEN); } catch (err) { /* nic */ }
    $('token').value = '';
    stateSha = null;
    setSyncStatus('Token zapomenut. Volby se dál ukládají jen v prohlížeči.');
    renderMeta();
  });
}

function start() {
  state = readLocalState();
  bindControls();

  fetch('data.json?t=' + Date.now(), { cache: 'no-store' })
    .then(function (res) {
      if (!res.ok) { throw new Error('data.json se nepodařilo načíst'); }
      return res.json();
    })
    .then(function (loaded) {
      data = {
        generated_at: loaded.generated_at,
        total_budget: loaded.total_budget,
        categories: Array.isArray(loaded.categories) ? loaded.categories : [],
        items: Array.isArray(loaded.items) ? loaded.items : []
      };
      renderMeta();
      render();
      return fetchRemoteState();
    })
    .then(function (remote) {
      state = mergeState(remote, state);
      writeLocalState();
      render();
    })
    .catch(function (err) {
      $('meta').textContent = 'Chyba: ' + err.message;
    });
}

start();
