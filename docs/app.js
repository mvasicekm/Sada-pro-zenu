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

// Parametry kola, které stránka nechá doplnit ručně - ve specifikaci
// výrobce nejsou a rozhodují o tom, co se na kolo vejde.
var BIKE_FIELDS = ['freehub', 'bb_shell', 'fd_mount', 'frame_size'];
var MAX_NAME = 60;

var data = { items: [], categories: [], total_budget: null, bike: {} };
var state = emptyState();
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

/* ---------- stav ----------
 *
 * Tvar: { items: {uid: {flag, at}}, builds: {id: {...}}, bike: {values, at} }
 * Všechno nese časovou značku, aby se dvě zařízení dala sloučit bez ztráty.
 */

function emptyState() {
  return { items: {}, builds: {}, bike: { values: {}, at: '' } };
}

function now() { return new Date().toISOString(); }

/** Očistí stav z cizího zdroje (soubor, API, starší verze stránky). */
function normalizeState(raw) {
  if (!raw || typeof raw !== 'object') { return emptyState(); }
  // Původní tvar byl rovnou mapa uid -> {flag, at}, bez sad a kola.
  var items = raw.items && typeof raw.items === 'object' ? raw.items : raw;
  var bike = raw.bike && typeof raw.bike === 'object' ? raw.bike : {};
  var values = bike.values && typeof bike.values === 'object' ? bike.values : {};

  var cleanItems = {};
  Object.keys(items).forEach(function (uid) {
    var entry = items[uid];
    if (!entry || typeof entry !== 'object') { return; }
    if (['like', 'skip', 'none'].indexOf(entry.flag) === -1) { return; }
    cleanItems[uid] = { flag: entry.flag, at: String(entry.at || '') };
  });

  var cleanBuilds = {};
  var builds = raw.builds && typeof raw.builds === 'object' ? raw.builds : {};
  Object.keys(builds).forEach(function (id) {
    var build = builds[id];
    if (!build || typeof build !== 'object') { return; }
    cleanBuilds[id] = {
      id: id,
      name: String(build.name || '').slice(0, MAX_NAME),
      uids: Array.isArray(build.uids) ? build.uids.map(String) : [],
      at: String(build.at || ''),
      deleted: !!build.deleted
    };
  });

  var cleanValues = {};
  BIKE_FIELDS.forEach(function (field) {
    if (values[field]) { cleanValues[field] = String(values[field]).slice(0, 20); }
  });

  return {
    items: cleanItems,
    builds: cleanBuilds,
    bike: { values: cleanValues, at: String(bike.at || '') }
  };
}

function readLocalState() {
  try {
    var raw = localStorage.getItem(LS_STATE);
    return normalizeState(raw ? JSON.parse(raw) : null);
  } catch (err) {
    return emptyState();
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

/** U každého záznamu vyhraje novější časová značka. */
function mergeByStamp(a, b) {
  var out = {};
  [a || {}, b || {}].forEach(function (source) {
    Object.keys(source).forEach(function (key) {
      var entry = source[key];
      var current = out[key];
      if (!current || String(entry.at || '') > String(current.at || '')) {
        out[key] = entry;
      }
    });
  });
  return out;
}

/** Sloučí dva stavy. Smazané sady zůstávají jako značka, aby se smazání
 *  přeneslo i na druhé zařízení a sada tam znovu nevyskočila. */
function mergeState(a, b) {
  var newerBike = String(b.bike.at || '') > String(a.bike.at || '') ? b.bike : a.bike;
  return {
    items: mergeByStamp(a.items, b.items),
    builds: mergeByStamp(a.builds, b.builds),
    bike: { values: newerBike.values || {}, at: newerBike.at || '' }
  };
}

function touch() {
  writeLocalState();
  render();
  schedulePush();
}

function flagOf(uid) {
  return state.items[uid] ? state.items[uid].flag : null;
}

function setFlag(uid, flag) {
  // Odkliknutí se neukládá jako smazání, ale jako 'none'. Bez té značky
  // by se při sloučení s jiným zařízením vrátil starý příznak zpátky.
  state.items[uid] = { flag: flagOf(uid) === flag ? 'none' : flag, at: now() };
  touch();
}

/* ---------- sady ---------- */

function liveBuilds() {
  return Object.keys(state.builds)
    .map(function (id) { return state.builds[id]; })
    .filter(function (build) { return !build.deleted; })
    .sort(function (x, y) { return String(x.at).localeCompare(String(y.at)); });
}

function addBuild() {
  var id = 'b' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
  state.builds[id] = {
    id: id,
    name: 'Sada ' + (liveBuilds().length + 1),
    uids: [],
    at: now(),
    deleted: false
  };
  touch();
}

function updateBuild(id, change) {
  var build = state.builds[id];
  if (!build) { return; }
  Object.keys(change).forEach(function (key) { build[key] = change[key]; });
  build.at = now();
  touch();
}

function toggleBuildItem(id, uid) {
  var build = state.builds[id];
  if (!build) { return; }
  var at = build.uids.indexOf(uid);
  var uids = build.uids.slice();
  if (at === -1) { uids.push(uid); } else { uids.splice(at, 1); }
  updateBuild(id, { uids: uids });
}

/* ---------- profil kola ---------- */

/** Profil ze specifikace doplněný o to, co uživatel dovyplnil na stránce. */
function effectiveBike() {
  var bike = {};
  Object.keys(data.bike || {}).forEach(function (key) { bike[key] = data.bike[key]; });
  BIKE_FIELDS.forEach(function (field) {
    var value = state.bike.values[field];
    if (value) { bike[field] = value; }
  });
  return bike;
}

function setBikeField(field, value) {
  var values = {};
  BIKE_FIELDS.forEach(function (key) {
    if (state.bike.values[key]) { values[key] = state.bike.values[key]; }
  });
  if (value) { values[field] = value; } else { delete values[field]; }
  state.bike = { values: values, at: now() };
  touch();
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
  return normalizeState(JSON.parse(text));
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
      .catch(function () { return emptyState(); });
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
    updated_at: now(),
    items: state.items,
    builds: state.builds,
    bike: state.bike
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

/* ---------- kolo: co se vejde ---------- */

function fitLines() {
  var bike = effectiveBike();
  var lines = [];

  if (bike.freehub === '11' || bike.freehub === '11s') {
    lines.push(['ok', 'Kazeta: ořech je 11rychlostní, takže 11s i 12s silniční'
      + ' kazeta se na náboj nasune.']);
  } else if (bike.freehub) {
    lines.push(['bad', 'Kazeta: ořech je ' + bike.freehub + 'rychlostní \u2013'
      + ' 11s a 12s kazeta se na něj nevejde. Buď zůstaň na 10s, nebo počítej'
      + ' i s novým nábojem/zapletením.']);
  } else {
    lines.push(['ask', 'Kazeta: nevíš, jaký je vzadu ořech. Kolo je z výroby'
      + ' 9rychlostní, takže tohle rozhoduje, jestli je 11s/12s vůbec možné.'
      + ' Zjisti to první \u2013 než cokoliv koupíš.']);
  }

  var cogs = bike.rd_max_cog || {};
  var models = Object.keys(cogs).sort(function (a, b) { return cogs[a] - cogs[b]; });
  if (models.length) {
    lines.push(['info', 'Největší pastorek: limit dává přehazovačka \u2013 '
      + models.map(function (m) {
        return m.toUpperCase() + ' do ' + cogs[m] + 'z';
      }).join(', ')
      + '. Větší kazeta dře o klec. Dnes máš ' + (bike.current_cogs || []).join('\u2013')
      + '.']);
  }

  if (bike.fd_max_capacity) {
    lines.push(['info', 'Převodníky: s přesmykačem smí být rozdíl nejvýš '
      + bike.fd_max_capacity + ' zubů \u2013 50/34 i 48/32 projde, 53/34 ne.'
      + ' U 1\u00d7 tohle neplatí.']);
  }

  var lengths = bike.crank_length_by_size || {};
  if (bike.frame_size && lengths[bike.frame_size]) {
    lines.push(['ok', 'Kliky: rám ' + bike.frame_size + ' \u2192 hledej '
      + String(lengths[bike.frame_size]).replace('.', ',') + ' mm.']);
  } else {
    lines.push(['ask', 'Kliky: délka podle velikosti rámu ('
      + Object.keys(lengths).map(function (size) {
        return size + ' ' + String(lengths[size]).replace('.', ',');
      }).join(', ') + ' mm). Doplň velikost rámu.']);
  }

  if (bike.bb_shell) {
    lines.push(['ok', 'Střed: ' + (bike.bb_shell === 'bsa' ? 'BSA se závitem'
      : 'PressFit') + ', k tomu kupuj odpovídající Hollowtech II střed.']);
  } else {
    lines.push(['ask', 'Střed: Shimano kliky potřebují Hollowtech II střed,'
      + ' kolo má z výroby FSA cartridge. Zjisti, jestli je rám BSA, nebo PressFit.']);
  }

  if (bike.fd_mount) {
    lines.push(['ok', 'Přesmykač: ' + (bike.fd_mount === 'braze-on'
      ? 'braze-on (na čep)' : 'na objímku') + '.']);
  } else {
    lines.push(['ask', 'Přesmykač: braze-on a objímka nejsou zaměnitelné.'
      + ' Podívej se na sedlovou trubku.']);
  }

  lines.push(['info', 'Brzdy: rám i vidlice jsou flat mount kotouč, kotouče '
    + (bike.rotor_mm || 160) + ' mm \u2013 ráfkové třmeny nejde přišroubovat'
    + ' a k 160 mm kotouči patří adaptér.']);

  return lines;
}

function renderBike() {
  $('bike-name').textContent = (data.bike && data.bike.name)
    ? data.bike.name + ' \u00b7 dnes ' + (data.bike.current_speeds || '?')
      + 'rychlostní, ' + (data.bike.current_chainrings || []).join('/')
      + ' vepředu' : '';

  var list = $('bike-fit');
  list.textContent = '';
  fitLines().forEach(function (line) {
    var row = document.createElement('li');
    row.className = 'fit__row fit__row--' + line[0];
    row.textContent = line[1];
    list.appendChild(row);
  });

  BIKE_FIELDS.forEach(function (field) {
    var select = $('bike-' + field);
    if (select) { select.value = state.bike.values[field] || ''; }
  });
}

/* ---------- sady ---------- */

function issueList(issues, kind, heading) {
  if (!issues.length) { return null; }
  var box = document.createElement('div');
  box.className = 'issues issues--' + kind;
  var title = document.createElement('h4');
  title.textContent = heading + ' (' + issues.length + ')';
  box.appendChild(title);
  var list = document.createElement('ul');
  issues.forEach(function (issue) {
    var row = document.createElement('li');
    var strong = document.createElement('strong');
    strong.textContent = issue.title;
    row.appendChild(strong);
    var detail = document.createElement('span');
    detail.textContent = ' ' + issue.detail;
    row.appendChild(detail);
    list.appendChild(row);
  });
  box.appendChild(list);
  return box;
}

/** Nabídka dílů do sady: co máš nasrdíčkované + co už v sadě je. */
function candidates(build) {
  return data.items.filter(function (item) {
    return flagOf(item.uid) === 'like' || build.uids.indexOf(item.uid) !== -1;
  });
}

function buildPicker(build) {
  var box = document.createElement('div');
  box.className = 'parts';

  var options = candidates(build);
  if (!options.length) {
    var hint = document.createElement('p');
    hint.className = 'muted';
    hint.textContent = 'Nejdřív si v seznamu dole zaklikej srdíčkem, co tě'
      + ' zajímá. Pak si z toho tady poskládáš sadu.';
    box.appendChild(hint);
    return box;
  }

  categoryOrder().forEach(function (key) {
    var picked = options.filter(function (item) { return item.category_key === key; });
    if (!picked.length) { return; }

    var group = document.createElement('div');
    group.className = 'parts__group';
    var label = document.createElement('span');
    label.className = 'pill';
    label.textContent = categoryInfo(key).label;
    group.appendChild(label);

    picked.forEach(function (item) {
      var row = document.createElement('label');
      row.className = 'check check--part';
      var box2 = document.createElement('input');
      box2.type = 'checkbox';
      box2.checked = build.uids.indexOf(item.uid) !== -1;
      box2.addEventListener('change', function () {
        toggleBuildItem(build.id, item.uid);
      });
      var text = document.createElement('span');
      text.textContent = (item.title || '(bez názvu)') + ' \u2013 ' + money(item.price);
      var specs = specLabel(item);
      if (specs) {
        var note = document.createElement('em');
        note.textContent = ' ' + specs;
        text.appendChild(note);
      }
      row.appendChild(box2);
      row.appendChild(text);
      group.appendChild(row);
    });
    box.appendChild(group);
  });
  return box;
}

/** Krátký výpis rozpoznaných parametrů, ať je vidět, z čeho kontrola vychází. */
function specLabel(item) {
  var specs = item.specs || {};
  var bits = [];
  if (specs.part && Compat.PART_LABELS[specs.part]) {
    bits.push(Compat.PART_LABELS[specs.part]);
  }
  if (specs.front && specs.speeds) {
    bits.push(specs.front + '\u00d7' + specs.speeds);
  } else if (specs.speeds) {
    bits.push(specs.speeds + '\u00d7');
  }
  if (specs.cog_min && specs.cog_max) {
    bits.push(specs.cog_min + '\u2013' + specs.cog_max + 'z');
  }
  if (specs.chainring_big && specs.chainring_small) {
    bits.push(specs.chainring_big + '/' + specs.chainring_small);
  }
  if (specs.brake === 'disc-hydraulic') { bits.push('hydraulika'); }
  if (specs.brake === 'rim') { bits.push('ráfková'); }
  if (specs.electronic) { bits.push('Di2'); }
  return bits.length ? '(' + bits.join(', ') + ')' : '';
}

function buildCardFor(build) {
  var items = data.items.filter(function (item) {
    return build.uids.indexOf(item.uid) !== -1;
  });
  var sum = 0;
  items.forEach(function (item) { sum += item.price || 0; });
  var result = Compat.checkBuild(items, effectiveBike());
  var budget = data.total_budget;

  var card = document.createElement('article');
  card.className = 'build';
  if (result.hard.length) { card.className += ' build--bad'; }
  else if (items.length) { card.className += ' build--ok'; }

  var head = document.createElement('div');
  head.className = 'build__head';

  var name = document.createElement('input');
  name.type = 'text';
  name.className = 'build__name';
  name.value = build.name;
  name.maxLength = MAX_NAME;
  name.setAttribute('aria-label', 'Název sady');
  name.addEventListener('change', function () {
    updateBuild(build.id, { name: name.value.slice(0, MAX_NAME) });
  });
  head.appendChild(name);

  var price = document.createElement('strong');
  price.className = 'build__price' + (budget && sum > budget ? ' over' : '');
  price.textContent = money(sum);
  head.appendChild(price);

  var remove = document.createElement('button');
  remove.type = 'button';
  remove.className = 'btn btn--ghost';
  remove.textContent = 'Smazat';
  remove.addEventListener('click', function () {
    updateBuild(build.id, { deleted: true });
  });
  head.appendChild(remove);
  card.appendChild(head);

  var status = document.createElement('p');
  status.className = 'build__status';
  if (!items.length) {
    status.textContent = 'Prázdná sada \u2013 zaškrtni níž díly, které do ní patří.';
  } else if (result.hard.length) {
    status.textContent = 'Takhle to nepojede, ' + result.hard.length
      + '\u00d7 to drhne.';
  } else {
    status.textContent = items.length + ' dílů, kontrola neprošla na nic'
      + ' zásadního.'
      + (budget ? (sum > budget ? ' Rozpočet přetažený o ' + money(sum - budget) + '.'
        : ' V rozpočtu, zbývá ' + money(budget - sum) + '.') : '');
  }
  card.appendChild(status);

  [
    [result.hard, 'hard', 'Nepojede to'],
    [result.soft, 'soft', 'Asi problém'],
    [result.verify, 'verify', 'Doptat se / ověřit']
  ].forEach(function (spec) {
    var box = issueList(spec[0], spec[1], spec[2]);
    if (box) { card.appendChild(box); }
  });

  card.appendChild(buildPicker(build));
  return card;
}

function renderBuilds() {
  var host = $('builds');
  host.textContent = '';
  var builds = liveBuilds();
  builds.forEach(function (build) { host.appendChild(buildCardFor(build)); });

  var liked = data.items.filter(function (i) { return flagOf(i.uid) === 'like'; });
  if (!builds.length) {
    $('builds-note').textContent = 'Založ si sadu pro každou variantu, kterou'
      + ' zvažuješ (105 A, GRX A, GRX B\u2026). U každé se spočítá cena a'
      + ' zkontroluje, jestli do sebe díly pasují a vejdou se na kolo.'
      + (liked.length ? '' : ' Nejdřív si ale zaklikej srdíčka v seznamu dole.');
  } else {
    $('builds-note').textContent = builds.length + ' sad \u00b7 na výběr máš '
      + liked.length + ' nasrdíčkovaných dílů.';
  }
}

function render() {
  renderCalc();
  renderBike();
  renderBuilds();
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

  $('build-add').addEventListener('click', addBuild);

  $('bike-toggle').addEventListener('click', function () {
    var form = $('bike-form');
    form.hidden = !form.hidden;
    $('bike-toggle').textContent = form.hidden ? 'Doplnit, co nevím' : 'Hotovo';
  });

  BIKE_FIELDS.forEach(function (field) {
    var select = $('bike-' + field);
    if (!select) { return; }
    select.addEventListener('change', function () {
      setBikeField(field, select.value);
    });
  });

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
        items: Array.isArray(loaded.items) ? loaded.items : [],
        bike: loaded.bike && typeof loaded.bike === 'object' ? loaded.bike : {}
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
