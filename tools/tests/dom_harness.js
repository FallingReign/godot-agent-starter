/* A DOM small enough to reason about, so the shipped page can be executed.
 *
 * "Test the artefact, not the mechanism": this harness does not import the
 * Python that generates the page. It reads the generated HTML file, pulls out
 * the <script> blocks that were actually written into it, and runs them
 * against a stub DOM and a stub fetch. If the generator stops emitting a
 * handler, or emits one that leaves a button disabled, that shows up here.
 *
 *     node tools/tests/dom_harness.js retro.html
 *
 * Prints one JSON object per scenario to stdout; exit 1 if any assertion
 * failed. Deliberately dependency-free -- no jsdom, no puppeteer, no npm at
 * all, matching the vendored/offline rule the pages themselves follow.
 */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

/* ------------------------------------------------------------------ parser */
const VOID = new Set(['meta', 'link', 'br', 'hr', 'img', 'input', 'source', 'area', 'base', 'col']);
const RAW = new Set(['script', 'style', 'textarea']);

function decode(s) {
  return s.replace(/&lt;/g, '<').replace(/&gt;/g, '>')
          .replace(/&quot;/g, '"').replace(/&#9744;/g, '\u2610')
          .replace(/&#9745;/g, '\u2611').replace(/&mdash;/g, '\u2014')
          .replace(/&middot;/g, '\u00b7').replace(/&times;/g, '\u00d7')
          .replace(/&amp;/g, '&');
}

class El {
  constructor(tag) {
    this.tagName = String(tag || '').toUpperCase();
    this.tag = String(tag || '').toLowerCase();
    this.attrs = {};
    this.children = [];
    this.parentNode = null;
    this._text = '';
    this._listeners = {};
    this.style = {};
    this.disabled = false;
    this.hidden = false;
    this.open = false;
    const self = this;
    this.classList = {
      contains(c) { return self._classes().indexOf(c) >= 0; },
      add(c) { const l = self._classes(); if (l.indexOf(c) < 0) { l.push(c); self.attrs['class'] = l.join(' '); } },
      remove(c) { self.attrs['class'] = self._classes().filter(x => x !== c).join(' '); },
      toggle(c, on) { if (on === undefined) on = !this.contains(c); on ? this.add(c) : this.remove(c); }
    };
  }
  _classes() {
    const raw = (this.attrs['class'] || '').trim();
    if (!raw) { return []; }
    const list = raw.split(/\s+/);
    return list;
  }
  get className() { return this.attrs['class'] || ''; }
  set className(v) { this.attrs['class'] = String(v); }
  get id() { return this.attrs['id'] || ''; }
  getAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attrs, name) ? this.attrs[name] : null; }
  setAttribute(name, v) { this.attrs[name] = String(v); }
  appendChild(child) {
    if (child.parentNode) { child.parentNode.removeChild(child); }
    child.parentNode = this;
    this.children.push(child);
    return child;
  }
  removeChild(child) {
    this.children = this.children.filter(c => c !== child);
    child.parentNode = null;
    return child;
  }
  remove() { if (this.parentNode) { this.parentNode.removeChild(this); } }
  focus() {}
  addEventListener(type, fn) { (this._listeners[type] = this._listeners[type] || []).push(fn); }
  fire(type) { (this._listeners[type] || []).forEach(fn => fn({type: type, target: this})); }
  get textContent() {
    if (RAW.has(this.tag)) { return this._text; }
    return this.children.map(c => (c instanceof El ? c.textContent : c.text)).join('') + this._text;
  }
  set textContent(v) {
    this.children = [];
    this._text = String(v);
  }
  get innerHTML() {
    if (this._html !== undefined) { return this._html; }
    return serialize(this);
  }
  set innerHTML(v) {
    this._html = String(v);
    this.children = [];
    this._text = '';
    parseInto(String(v), this);
  }
  _all(out) {
    for (const c of this.children) {
      if (c instanceof El) { out.push(c); c._all(out); }
    }
    return out;
  }
  querySelectorAll(sel) { return this._all([]).filter(el => matches(el, sel)); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
}

/* One compound simple selector: tag, .class, #id, [attr], [attr="v"], and
 * combinations of those. Descendant combinators are supported by matching the
 * right-hand compound and then walking up for the rest. */
function serialize(el) {
  let out = '';
  for (const c of el.children) {
    out += '<' + c.tag;
    for (const k of Object.keys(c.attrs)) { out += ' ' + k + '="' + c.attrs[k] + '"'; }
    out += '>' + serialize(c) + '</' + c.tag + '>';
  }
  return out + el._text;
}

function matches(el, sel) {
  const re = /([.#]?[a-zA-Z][\w-]*)|(\[[^\]]+\])/g;
  let m;
  while ((m = re.exec(sel)) !== null) {
    const tok = m[0];
    if (tok[0] === '.') {
      if (!el.classList.contains(tok.slice(1))) { return false; }
    } else if (tok[0] === '#') {
      if (el.id !== tok.slice(1)) { return false; }
    } else if (tok[0] === '[') {
      const inner = tok.slice(1, -1);
      const eq = inner.indexOf('=');
      if (eq < 0) {
        if (el.getAttribute(inner) === null) { return false; }
      } else {
        const name = inner.slice(0, eq);
        const want = inner.slice(eq + 1).replace(/^["']|["']$/g, '');
        if (el.getAttribute(name) !== want) { return false; }
      }
    } else if (el.tag !== tok.toLowerCase()) {
      return false;
    }
  }
  return true;
}

function parseInto(html, root) {
  let stack = [root];
  let i = 0;
  while (i < html.length) {
    const lt = html.indexOf('<', i);
    if (lt < 0) { break; }
    if (lt > i) { stack[stack.length - 1]._text += decode(html.slice(i, lt)); }
    if (html.startsWith('<!--', lt)) {
      const end = html.indexOf('-->', lt);
      i = end < 0 ? html.length : end + 3;
      continue;
    }
    if (html[lt + 1] === '!') {
      const end = html.indexOf('>', lt);
      i = end < 0 ? html.length : end + 1;
      continue;
    }
    if (html[lt + 1] === '/') {
      const end = html.indexOf('>', lt);
      const tag = html.slice(lt + 2, end).trim().toLowerCase();
      for (let k = stack.length - 1; k > 0; k--) {
        if (stack[k].tag === tag) { stack = stack.slice(0, k); break; }
      }
      i = end + 1;
      continue;
    }
    const end = findTagEnd(html, lt);
    const raw = html.slice(lt + 1, end);
    const selfClosing = raw.endsWith('/');
    const sp = raw.search(/[\s/]/);
    const tag = (sp < 0 ? raw : raw.slice(0, sp)).toLowerCase();
    const el = new El(tag);
    const attrRe = /([a-zA-Z_:][-\w:.]*)(?:\s*=\s*("([^"]*)"|'([^']*)'|([^\s"'>]+)))?/g;
    const attrsSrc = sp < 0 ? '' : raw.slice(sp);
    let am;
    while ((am = attrRe.exec(attrsSrc)) !== null) {
      const val = am[3] !== undefined ? am[3] : (am[4] !== undefined ? am[4] : (am[5] !== undefined ? am[5] : ''));
      el.attrs[am[1]] = decode(val);
    }
    if (Object.prototype.hasOwnProperty.call(el.attrs, 'hidden')) { el.hidden = true; }
    stack[stack.length - 1].appendChild(el);
    i = end + 1;
    if (VOID.has(tag) || selfClosing) { continue; }
    if (RAW.has(tag)) {
      const close = html.toLowerCase().indexOf('</' + tag, i);
      const stop = close < 0 ? html.length : close;
      el._text = tag === 'script' || tag === 'style' ? html.slice(i, stop) : decode(html.slice(i, stop));
      i = close < 0 ? html.length : html.indexOf('>', close) + 1;
      continue;
    }
    stack.push(el);
  }
  if (i < html.length) { stack[stack.length - 1]._text += decode(html.slice(i)); }
}

function findTagEnd(html, from) {
  let quote = null;
  for (let i = from + 1; i < html.length; i++) {
    const c = html[i];
    if (quote) { if (c === quote) { quote = null; } continue; }
    if (c === '"' || c === "'") { quote = c; continue; }
    if (c === '>') { return i; }
  }
  return html.length;
}

/* ---------------------------------------------------------------- document */
function buildDocument(html) {
  const docRoot = new El('#document');
  parseInto(html, docRoot);
  const body = docRoot.querySelector('body') || docRoot;
  const doc = {
    readyState: 'complete',
    hidden: false,
    body: body,
    _listeners: {},
    getElementById(id) { return docRoot.querySelector('#' + id); },
    querySelector(sel) { return docRoot.querySelector(sel); },
    querySelectorAll(sel) { return docRoot.querySelectorAll(sel); },
    createElement(tag) { return new El(tag); },
    addEventListener(type, fn) { (doc._listeners[type] = doc._listeners[type] || []).push(fn); },
    fire(type) { (doc._listeners[type] || []).forEach(fn => fn({type: type})); }
  };
  return {doc: doc, root: docRoot};
}

function scriptsOf(html) {
  const out = [];
  const re = /<script\b([^>]*)>([\s\S]*?)<\/script>/gi;
  let m;
  while ((m = re.exec(html)) !== null) {
    if (/\bsrc\s*=/.test(m[1])) { continue; }   // vendored mermaid, not ours
    out.push(m[2]);
  }
  return out;
}

/* ---------------------------------------------------------------- scenarios */
function response(status, body, ok) {
  return {ok: ok === undefined ? (status >= 200 && status < 300) : ok,
          status: status, text: () => Promise.resolve(body)};
}

function makeFetch(routes, log) {
  return function (url, init) {
    log.push({url: url, method: (init && init.method) || 'GET'});
    const r = routes[url] || routes['*'];
    if (typeof r === 'function') { return r(url, init); }
    if (r === undefined) { return Promise.reject(new Error('no route: ' + url)); }
    return Promise.resolve(r);
  };
}

function run(html, opts) {
  const built = buildDocument(html);
  const log = [];
  const timers = [];
  const ctx = {
    console: {log() {}, warn() {}, error() {}},
    document: built.doc,
    location: {protocol: opts.protocol || 'http:', origin: 'http://127.0.0.1:8899', hash: ''},
    fetch: makeFetch(opts.routes || {}, log),
    setTimeout(fn, ms) { timers.push({fn: fn, ms: ms}); return timers.length; },
    clearTimeout() {},
    setInterval() { return 0; },
    encodeURIComponent: encodeURIComponent
  };
  ctx.window = ctx;
  ctx.globalThis = ctx;
  ctx.addEventListener = function () {};      // plan.html's hashchange handler
  ctx.removeEventListener = function () {};
  vm.createContext(ctx);
  for (const src of scriptsOf(html)) {
    try { vm.runInContext(src, ctx); }
    catch (e) { return Promise.reject(new Error('script threw: ' + e.message)); }
  }
  return settle().then(() => ({ctx: ctx, doc: built.doc, log: log, timers: timers}));
}

function settle(n) {
  n = n || 40;
  let p = Promise.resolve();
  for (let i = 0; i < n; i++) { p = p.then(() => undefined); }
  return p;
}

/* ------------------------------------------------------------------ asserts */
const results = [];
function check(scenario, name, cond, detail) {
  results.push({scenario: scenario, name: name, pass: !!cond, detail: detail || ''});
}

const HEALTH_OK = () => response(200, JSON.stringify({ok: true, port: 8899, pid: 1, schema: 1}));
function stateBody(overrides) {
  return JSON.stringify(Object.assign({
    board: {port: 8899, pid: 1, schema: 1},
    retro_due: {unarchived: 11, threshold: 10, due: true},
    findings: [],
    runs: []
  }, overrides || {}));
}

function firstSlug(doc) {
  const card = doc.querySelector('.finding[data-slug]');
  return card ? card.getAttribute('data-slug') : '';
}

async function main() {
  const file = process.argv[2] || 'retro.html';
  const html = fs.readFileSync(path.resolve(file), 'utf8');
  /* The retro page is identified by the element, not by the string: plan.html
     renders docs inline and can quite legitimately *mention* #list-toaction. */
  const isRetro = /<div id="list-toaction"/.test(html);

  /* ---------------------------------------------------- 1. healthy board */
  {
    const probe = buildDocument(html);
    const slug = firstSlug(probe.doc);
    const findings = [
      {slug: slug, title: 'a', severity: 'high', state: 'working', comment: 'Do the small one first.',
       stale: false, run_id: 'r1', status_detail: 'started 34 min ago', updated_at: ''},
      {slug: 'not-rendered-on-this-page', title: 'b', severity: 'none', state: 'awaiting_review',
       comment: '', stale: false, run_id: null, status_detail: '', updated_at: ''}
    ];
    const runs = [{run_id: 'r1', status: 'running', silence_minutes: 27,
                   resume_cmd: 'copilot --resume abc', status_label: 'no output 27m',
                   elapsed_s: 2040, persona: 'kit-builder'}];
    const env = await run(html, {routes: {
      '/api/health': HEALTH_OK(),
      '/api/state': response(200, stateBody({findings: findings, runs: runs}))
    }});
    const S = 'healthy';
    check(S, 'body is board-live', env.doc.body.classList.contains('board-live'));
    check(S, 'banner is silent when live',
          (env.doc.getElementById('board-banner') || {innerHTML: ''}).innerHTML === '');
    if (isRetro) {
      const card = env.doc.querySelector('.finding[data-slug="' + slug + '"]');
      check(S, 'working card moved to the approved list',
            card && card.parentNode && card.parentNode.id === 'list-approved');
      const slot = card && card.querySelector('.status-slot');
      check(S, 'a silent worker is rendered as stalled, not as progress',
            slot && slot.classList.contains('stalled'), slot ? slot.className : 'no slot');
      check(S, 'stalled item states the silence in words',
            slot && /silent 27 min/.test(slot.innerHTML), slot ? slot.innerHTML.slice(0, 120) : '');
      check(S, 'stalled item shows the resume command',
            slot && /copilot --resume abc/.test(slot.innerHTML));
      check(S, 'approval comment is shown on the approved item',
            slot && /Do the small one first\./.test(slot.innerHTML));
      const form = card && card.querySelector('.decision-form');
      check(S, 'settled item hides its approve form', !form || form.style.display === 'none');
      const list = env.doc.getElementById('list-toaction');
      const open = list ? list.querySelector('.approve-btn') : null;
      check(S, 'an unsettled approve button is enabled', open ? open.disabled === false : true);

      /* ---- tabs: exactly one panel visible, ever ---- */
      const panelsBefore = env.doc.querySelectorAll('.panel').filter(p => !p.hidden);
      check(S, 'exactly one panel is visible before any tab click', panelsBefore.length === 1,
            String(panelsBefore.length));
      const tabs = env.doc.querySelectorAll('.tab');
      const secondTab = tabs[1];
      if (secondTab) {
        secondTab.fire('click');
        const panelsAfter = env.doc.querySelectorAll('.panel').filter(p => !p.hidden);
        check(S, 'switching tabs shows exactly one panel', panelsAfter.length === 1,
              String(panelsAfter.length));
        check(S, 'the panel shown is the one the clicked tab names',
              panelsAfter[0] && panelsAfter[0].id === secondTab.getAttribute('data-p'));
        tabs[0].fire('click');   // restore to-action visible for the checks below
      }

      /* ---- the wedged banner sits above the tabs ---- */
      const wedgeBanner = env.doc.getElementById('wedge-banner');
      const tabsBar = env.doc.querySelector('.tabs');
      check(S, 'the wedged banner element exists above the tab bar',
            !!wedgeBanner && !!tabsBar);

      /* ---- only one text field visible per action card until Defer ---- */
      const isVisible = el => { let n = el; while (n) { if (n.hidden) { return false; } n = n.parentNode; } return true; };
      const form0 = env.doc.querySelector('.decision-form');
      if (form0) {
        const inputsOf = () => form0.querySelectorAll('textarea')
          .concat(form0.querySelectorAll('input')).filter(isVisible);
        check(S, 'only one text field is visible per action card before Defer is clicked',
              inputsOf().length === 1, String(inputsOf().length));
        const deferToggle = form0.querySelector('[data-defer]');
        if (deferToggle) {
          deferToggle.fire('click');
          // The comment box is always present (it is the conscious decision
          // behind approval); the approved mock's own JS only toggles the
          // defer button and the deferbox, so once open the defer reason
          // input joins it -- one *new* field is revealed, not swapped in.
          check(S, 'the defer box reveals its own single input',
                !form0.querySelector('.deferbox').hidden
                && form0.querySelector('.defer-reason') !== null,
                String(inputsOf().length));
          check(S, 'the defer toggle button hides itself once clicked',
                form0.querySelector('[data-defer]').hidden === true);
        }
      }
    } else {
      const banner = env.doc.getElementById('retro-banner');
      check(S, 'plan banner announces the due retrospective',
            banner && /retrospective is due/i.test(banner.innerHTML), banner ? banner.innerHTML : 'missing');
      check(S, 'plan banner announces findings awaiting a decision',
            banner && /awaiting your decision/i.test(banner.innerHTML));
      check(S, 'plan banner links into the retro board',
            banner && /href="\/retro\.html"/.test(banner.innerHTML));
    }
  }

  /* --------------------------------------- 1b. ordering and partitioning */
  if (isRetro) {
    const S = 'ordering';
    const probe = buildDocument(html);
    const cards = probe.doc.querySelectorAll('.finding[data-slug]');
    const slugs = cards.map(c => c.getAttribute('data-slug'));
    const decided = {};
    cards.forEach(c => { decided[c.getAttribute('data-slug')] = c.getAttribute('data-decided') || ''; });
    const STATES = ['done', 'working', 'queued', 'failed', 'awaiting_review'];
    const findings = slugs.map((s, i) => ({
      slug: s, title: 't' + i, severity: 'none', state: STATES[i % STATES.length],
      comment: '', stale: false, run_id: null, status_detail: '',
      updated_at: '2026-01-0' + ((i % 8) + 1)
    }));
    const routes = f => ({
      '/api/health': HEALTH_OK(),
      '/api/state': response(200, stateBody({findings: f, runs: []}))
    });
    const forward = await run(html, {routes: routes(findings)});
    const backward = await run(html, {routes: routes(findings.slice().reverse())});

    const order = (env, id) => {
      const host = env.doc.getElementById(id);
      return host ? host.querySelectorAll('.finding').map(c => c.getAttribute('data-slug')) : null;
    };
    const rank = (env, id) => {
      const host = env.doc.getElementById(id);
      return host ? host.querySelectorAll('.rank').map(e => e.textContent.trim()) : [];
    };
    const LISTS = ['list-toaction', 'list-approved', 'list-deferred'];

    check(S, 'all three lists exist in the DOM',
          LISTS.every(id => order(forward, id) !== null));
    if (LISTS.some(id => order(forward, id) === null)) {
      throw new Error('retro page is missing one of ' + LISTS.join(', '));
    }
    check(S, 'every card is placed in exactly one list',
          LISTS.reduce((n, id) => n + order(forward, id).length, 0) === slugs.length,
          JSON.stringify(LISTS.map(id => order(forward, id))));

    const liveStates = {working: 1, stalled: 1, queued: 1, done: 1, failed: 1};
    const toaction = order(forward, 'list-toaction');
    const byState = {};
    findings.forEach(f => { byState[f.slug] = f.state; });
    check(S, 'no settled card is ever placed in the to-action list',
          toaction.every(s => !liveStates[byState[s]] && decided[s] !== 'approved'
                              && decided[s] !== 'deferred'),
          JSON.stringify(toaction.map(s => [s, byState[s], decided[s]])));
    const approved = order(forward, 'list-approved');
    check(S, 'every approved card is live or recorded as approved',
          approved.every(s => liveStates[byState[s]] || decided[s] === 'approved'),
          JSON.stringify(approved.map(s => [s, byState[s], decided[s]])));
    const rankOf = s => ({working: 0, stalled: 0, queued: 1, done: 2, failed: 3})[byState[s]];
    const ranksSeen = approved.map(s => (rankOf(s) === undefined ? 4 : rankOf(s)));
    check(S, 'approved runs live-first, then queued, done, failed',
          ranksSeen.every((v, i) => i === 0 || ranksSeen[i - 1] <= v),
          JSON.stringify(ranksSeen));

    check(S, 'the order does not depend on the order the board sent',
          LISTS.every(id => order(forward, id).join(',') === order(backward, id).join(',')),
          JSON.stringify(LISTS.map(id => [order(forward, id), order(backward, id)])));

    check(S, 'every card in a list is numbered by its position',
          LISTS.every(id => {
            const rs = rank(forward, id);
            return rs.every((v, i) => v === (i + 1) + '.');
          }),
          JSON.stringify(LISTS.map(id => rank(forward, id))));
  }

  /* --------------------------------------- 2. approve returns HTTP 500 */
  if (isRetro) {
    const S = 'approve-500';
    const env = await run(html, {routes: {
      '/api/health': HEALTH_OK(),
      '/api/state': response(200, stateBody()),
      '*': (url, init) => (init && init.method === 'POST')
        ? Promise.resolve(response(500, JSON.stringify({error: 'copilot not on PATH'})))
        : Promise.resolve(response(404, '{}'))
    }});
    const btn = env.doc.querySelector('.approve-btn');
    const before = btn.textContent;
    btn.fire('click');
    await settle();
    check(S, 'approve button is re-enabled after a 500', btn.disabled === false);
    check(S, 'approve button label is restored', btn.textContent === before, btn.textContent);
    const errs = env.doc.querySelectorAll('.board-error');
    check(S, 'a readable error is shown in the page', errs.length > 0);
    check(S, 'the error names the status and the reason',
          errs.length > 0 && /500/.test(errs[0].innerHTML) && /copilot not on PATH/.test(errs[0].innerHTML),
          errs.length ? errs[0].innerHTML : '');
    const form = btn.parentNode;
    check(S, 'the comment box is usable again',
          form.querySelector('.comment') ? form.querySelector('.comment').disabled === false : true);
  }

  /* ------------------------------------- 3. approve returns non-JSON */
  if (isRetro) {
    const S = 'approve-non-json';
    const env = await run(html, {routes: {
      '/api/health': HEALTH_OK(),
      '/api/state': response(200, stateBody()),
      '*': () => Promise.resolve(response(200, '<html><body>Traceback</body></html>'))
    }});
    const btn = env.doc.querySelector('.approve-btn');
    btn.fire('click');
    await settle();
    check(S, 'approve button is re-enabled after a non-JSON reply', btn.disabled === false);
    const errs = env.doc.querySelectorAll('.board-error');
    check(S, 'non-JSON is reported rather than swallowed',
          errs.length > 0 && /non-JSON/.test(errs[0].innerHTML), errs.length ? errs[0].innerHTML : '');
  }

  /* ------------------------------------- 4. prompt preview fails to load */
  if (isRetro) {
    const S = 'prompt-500';
    const env = await run(html, {routes: {
      '/api/health': HEALTH_OK(),
      '/api/state': response(200, stateBody()),
      '*': () => Promise.resolve(response(500, JSON.stringify({error: 'artifact unreadable'})))
    }});
    const det = env.doc.querySelector('.prompt-block');
    const pre = det.querySelector('pre');
    const inlinePrompt = pre.textContent;
    check(S, 'the artifact prompt is inlined so it is readable with no board',
          inlinePrompt.length > 100, String(inlinePrompt.length));
    det.open = true;
    det.fire('toggle');
    await settle();
    check(S, 'a failed preview leaves the inlined prompt in place', pre.textContent === inlinePrompt);
    check(S, 'a failed preview is reported', env.doc.querySelectorAll('.board-error').length > 0);
  }

  /* ----------------------------------------------- 5. board unreachable */
  {
    const S = 'board-down';
    const env = await run(html, {routes: {'*': () => Promise.reject(new Error('ECONNREFUSED'))}});
    check(S, 'body is marked board-down', env.doc.body.classList.contains('board-down'));
    const banner = env.doc.getElementById('board-banner');
    if (isRetro) {
      check(S, 'the page says the board is not reachable',
            banner && /not reachable/i.test(banner.innerHTML));
      check(S, 'the page names the URL it tried',
            banner && /127\.0\.0\.1:8899/.test(banner.innerHTML), banner ? banner.innerHTML : '');
      check(S, 'the page says how to bring the board back',
            banner && /board\.py --ensure/.test(banner.innerHTML));
      check(S, 'a retry control exists that does not need a reload',
            !!env.doc.getElementById('board-retry'));
      const ctrls = env.doc.querySelectorAll('[data-board-control]');
      check(S, 'every control is visibly disabled, not clickable-but-inert',
            ctrls.length > 0 && ctrls.every(c => c.disabled === true), String(ctrls.length));
    } else {
      const rb = env.doc.getElementById('retro-banner');
      check(S, 'plan banner degrades to nothing', !rb || rb.innerHTML === undefined || rb.innerHTML === '');
    }
    const polls = env.timers.filter(t => t.ms > 0);
    check(S, 'polling backs off rather than hammering a dead port',
          polls.length > 0 && polls[polls.length - 1].ms >= 10000,
          JSON.stringify(polls.map(t => t.ms)));
  }

  /* ------------------------------------------------- 6. file:// read-only */
  {
    const S = 'file-readonly';
    const env = await run(html, {protocol: 'file:', routes: {}});
    check(S, 'no request is attempted under file://', env.log.length === 0, JSON.stringify(env.log));
    check(S, 'no polling timer is armed under file://', env.timers.length === 0);
    if (isRetro) {
      const banner = env.doc.getElementById('board-banner');
      check(S, 'the page says it is read-only because it was opened as a file',
            banner && /Read-only/.test(banner.innerHTML) && /file:\/\//.test(banner.innerHTML),
            banner ? banner.innerHTML.slice(0, 160) : 'missing');
      check(S, 'it explains approval and dispatch need the loopback board',
            banner && /loopback board/.test(banner.innerHTML));
      const ctrls = env.doc.querySelectorAll('[data-board-control]');
      check(S, 'controls are disabled under file://',
            ctrls.length > 0 && ctrls.every(c => c.disabled === true));
      const pre = env.doc.querySelector('.prompt-block').querySelector('pre');
      check(S, 'the report including the prompt is still readable',
            pre && pre.textContent.length > 100);
    } else {
      const rb = env.doc.getElementById('retro-banner');
      check(S, 'plan banner stays empty under file://', !rb || !rb.innerHTML);
    }
  }

  const failed = results.filter(r => !r.pass);
  console.log(JSON.stringify({file: file, total: results.length,
                              failed: failed.length, results: results}, null, 1));
  process.exit(failed.length ? 1 : 0);
}

main().catch(e => { console.error(String(e && e.stack || e)); process.exit(2); });
