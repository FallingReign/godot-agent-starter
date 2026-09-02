"""Shared client-side glue for pages served by tools/board.py.

plan.html and retro.html both embed this identically, rather than each
growing its own copy, so there is exactly one place that decides how a page
talks to the board, how it reports the board being down, and how it renders a
run's status.

Three modes, and the page must say which one it is in. The previous revision
detected only two ("fetch worked" / "fetch did not") and, because a rejected
promise looks identical whether the protocol blocked it or the port is dead,
a page opened by double-clicking was indistinguishable from a page whose
board had exited. That was review finding 6. So `location.protocol` *is*
sniffed here, deliberately:

    file://          read-only. The report is readable; approval and dispatch
                     need the loopback board. Say so, print the URL.
    http:// + ok     live. Controls enabled.
    http:// + fail   board unreachable. Name the URL tried, say how to bring
                     it back, disable controls visibly, offer Retry.

Everything that talks to the board goes through `Board.request`, which checks
`response.ok`, survives a non-JSON body, and returns a result object instead
of throwing -- and `Board.guard`, which restores a control's enabled state in
a `finally`-equivalent so no code path can leave a button dead (review
finding 4).

No build step, no framework, no CDN: the JS below is inlined verbatim into
both generated pages and works from a file:// double-click with no network.
"""
from __future__ import annotations

import html
import hashlib
import json
import re
from pathlib import Path

import sys

TOOLS = Path(__file__).resolve().parent
CORE_ROOT = TOOLS.parent
sys.path.insert(0, str(TOOLS))
import project_context  # noqa: E402
import runtime_paths  # noqa: E402

ROOT = project_context.resolve_active_installation(CORE_ROOT).project_root
BOARD_STATE = runtime_paths.resolve(ROOT).board_state
PROTOCOL_SCHEMA = 2
PROTOCOL_FILES = (
    "board.py", "board_client.py", "cockpit.py", "providers.py", "run_result.py",
    "runtime_paths.py", "retro_queue.py", "retro_rank.py", "retro_due.py",
    "session_digest.py", "retro_html.py", "plan_html.py",
)

_SAFE_REVIEW_HINT = re.compile(
    r"http://127\.0\.0\.1:(?P<port>[0-9]{1,5})/(?:plan|retro)\.html"
)


def _is_safe_review_hint(value: str) -> bool:
    """Return whether ``value`` is one exact kit cockpit review URL.

    The link is deliberately narrower than a generic URL allowlist.  Generated
    file-mode pages may make only the canonical loopback plan and retro views
    clickable; snapshots, alternate hosts, credentials, suffixes, query
    strings and fragments remain visible text.
    """
    match = _SAFE_REVIEW_HINT.fullmatch(value)
    if match is None:
        return False
    port_text = match.group("port")
    port = int(port_text)
    return 1 <= port <= 65535 and port_text == str(port)


def _review_hint_html(value: str) -> str:
    """Render a review hint as one safe link or escaped inert text."""
    if not value:
        return ""
    escaped = html.escape(value, quote=True)
    if _is_safe_review_hint(value):
        return f'<a href="{escaped}">{escaped}</a>'
    return f"<code>{escaped}</code>"


def _inline_json(value: str) -> str:
    """Encode text for an inline script without permitting ``</script>``."""
    return (
        json.dumps(value, ensure_ascii=True)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def protocol_version() -> str:
    """Fingerprint the server/client contract so stale board code is refused."""
    digest = hashlib.sha256()
    for name in PROTOCOL_FILES:
        path = TOOLS / name
        try:
            content = path.read_bytes()
        except OSError:
            return "unavailable"
        digest.update(name.encode("utf-8") + b"\0" + content + b"\0")
    return digest.hexdigest()[:16]

NAV_CSS = """
.page-nav{display:flex;gap:12px;margin:0 0 18px;padding-bottom:10px;
border-bottom:1px solid var(--line);font-size:13px}
.page-nav a{color:var(--acc);text-decoration:none}
.page-nav a:hover{text-decoration:underline}
"""


def navigation_html(depth: int = 0) -> str:
    prefix = "../" * depth
    return (
        '<nav class="page-nav" aria-label="Generated pages">'
        f'<a href="{prefix}plan.html">Plan</a>'
        f'<a href="{prefix}retro.html">Retrospective</a>'
        "</nav>"
    )


def last_known_board_url() -> str:
    """The URL the board was last seen on, for a page opened under file://.

    Read from the state file rather than by importing board.py: board.py
    imports the page generators, which import this module, so an import back
    would be a real cycle. A stale value is fine and expected -- the page
    labels it as the URL to try and the health probe is what decides.
    """
    try:
        data = json.loads(BOARD_STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    port = data.get("port")
    return f"http://127.0.0.1:{port}/" if port else ""


CSS = """
.board-only{display:none}
.board-live .board-only{display:block}
.board-live .board-only.inline{display:inline-block}

#board-banner{margin:14px 0;padding:10px 14px;border-radius:7px;font-size:13px;
border:1px solid var(--line);background:var(--card);display:none}
#board-banner.show{display:block}
#board-banner.down{border-color:#5c2224;background:#241416}
#board-banner.readonly{border-color:#4a4326;background:#221f14}
#board-banner b{display:block;margin-bottom:4px}
#board-banner code{background:#0d1117;padding:1px 6px;border-radius:4px;user-select:all}
#board-banner .how a{color:var(--acc);overflow-wrap:anywhere}
#board-banner .how{color:var(--dim);margin-top:5px}
#board-banner button{margin-top:8px}

#board-errors{margin:10px 0;display:flex;flex-direction:column;gap:6px}
.board-error{padding:8px 12px;border:1px solid #5c2224;background:#241416;
border-radius:6px;font-size:12.5px;color:#f0c9c9}
.board-error .path{color:var(--dim);font-size:11.5px;display:block}
.board-error button{float:right;margin-left:10px}

#board-status{margin:14px 0;padding:10px 14px;background:var(--card);
border:1px solid var(--line);border-radius:7px;font-size:13px}
#board-status .run{margin:4px 0;display:flex;gap:10px;flex-wrap:wrap;align-items:baseline}
#board-status .run .label{font-weight:600;padding:1px 7px;border-radius:9px;
border:1px solid var(--line)}
#board-status .run .label.running{color:var(--acc);border-color:#2a5a8f}
#board-status .run .label.finished{color:var(--ok);border-color:#1f4d2b}
#board-status .run .label.failed{color:var(--bad);border-color:#5c2224}
#board-status .run code{background:#0d1117;padding:1px 6px;border-radius:4px;
user-select:all;font-size:12px}
#board-status .run .lastline{color:var(--dim);font-size:12px}

/* A control that cannot work must look like it cannot work, rather than
   being clickable-but-inert (review finding 6). */
button[disabled],textarea[disabled],input[disabled]{opacity:.45;cursor:not-allowed}
"""

# Polling. Modest by default, paused entirely while the tab is hidden, and
# backed off exponentially while the board is unreachable -- a page left open
# overnight next to a dead board should not hammer a closed port every 4s.
_POLL_MS = 5000
_POLL_MAX_MS = 60000


def core_js(board_url_hint: str = "") -> str:
    """The `Board` object, inlined into every generated page.

    `board_url_hint` is baked in so a page opened under file:// can still
    print the loopback URL it should have been opened on.
    """
    raw_hint = str(board_url_hint or "")
    hint = _inline_json(raw_hint)
    hint_html = _inline_json(_review_hint_html(raw_hint))
    version = json.dumps(protocol_version())
    return """
<script>
window.Board = (function(){
  var HINT = __HINT__, HINT_HTML = __HINT_HTML__;
  var EXPECTED_SCHEMA = __SCHEMA__, EXPECTED_VERSION = __VERSION__;
  var POLL_MS = __POLL__, POLL_MAX_MS = __POLLMAX__;
  var isFile = (location.protocol === 'file:');
  var mode = 'unknown';            // unknown | file | live | down
  var backoff = POLL_MS;
  var timer = null;
  var stateSubs = [];
  var lastState = null;

  function esc(s){
    return String(s == null ? '' : s)
      .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
      .replace(/"/g,'&quot;');
  }
  function byId(id){ return document.getElementById(id); }
  function boardUrl(){ return isFile ? (HINT || 'the loopback board') : location.origin + '/'; }
  function capability(){
    var meta = document.querySelector('meta[name="kit-board-token"]');
    return meta ? String(meta.getAttribute('content') || '') : '';
  }

  /* ------------------------------------------------------------ request
     Never throws, never returns a half-answer. Callers branch on .ok and
     show .error; there is no path that silently swallows a failure. */
  function request(path, opts){
    opts = opts || {};
    if(isFile){
      return Promise.resolve({ok:false, status:0, path:path,
        error:'This page was opened from a file, so the browser blocks '
            + 'requests to the board. Open it on the loopback board instead'
            + (HINT ? ' (' + HINT + ').' : '.')});
    }
    var method = String(opts.method || 'GET').toUpperCase();
    var init = {method: method};
    if(method !== 'GET'){
      var token = capability();
      if(!token){
        return Promise.resolve({ok:false, status:0, path:path,
          error:'This page has no live board capability. Reload it from the board URL.'});
      }
      init.headers = {'Content-Type':'application/json',
                      'X-Kit-Board-Token':token};
    }
    if(opts.body !== undefined){
      init.body = JSON.stringify(opts.body);
    }
    var status = 0;
    return fetch(path, init).then(function(r){
      status = r.status;
      return r.text().then(function(text){
        var data = null, parseError = null;
        if(text){
          try { data = JSON.parse(text); }
          catch(e){ parseError = text.slice(0, 200); }
        }
        if(!r.ok){
          var detail = (data && (data.error || data.detail)) || parseError
                        || ('HTTP ' + r.status);
          return {ok:false, status:status, path:path, data:data,
                  error:'Board returned ' + r.status + ': ' + detail};
        }
        if(parseError !== null){
          return {ok:false, status:status, path:path,
                  error:'Board sent a non-JSON reply: ' + parseError};
        }
        return {ok:true, status:status, path:path, data:(data === null ? {} : data)};
      });
    }).catch(function(e){
      return {ok:false, status:status, path:path,
              error:'Could not reach the board at ' + boardUrl() + ' \\u2014 '
                  + (e && e.message ? e.message : 'request failed')};
    });
  }

  /* ------------------------------------------------------------- guard
     Disables controls for the duration of an async action and restores
     them however it ends. This is the whole of review finding 4: there
     must be no code path that leaves a button permanently disabled. */
  function guard(controls, busyLabel, fn){
    controls = [].concat(controls).filter(Boolean);
    var prior = controls.map(function(c){
      return {el:c, disabled:!!c.disabled, text:c.textContent};
    });
    controls.forEach(function(c){ c.disabled = true; });
    if(busyLabel && controls[0] && controls[0].tagName === 'BUTTON'){
      controls[0].textContent = busyLabel;
    }
    function restore(){
      prior.forEach(function(p){
        // Re-disable rather than re-enable if the board went away while the
        // request was in flight: enabled-but-useless is the state finding 6
        // is about.
        p.el.disabled = (mode === 'live') ? p.disabled : true;
        if(p.el.tagName === 'BUTTON') p.el.textContent = p.text;
      });
    }
    var out;
    try { out = fn(); }
    catch(e){ restore(); showError(String((e && e.message) || e)); return Promise.resolve(null); }
    if(!out || typeof out.then !== 'function'){ restore(); return Promise.resolve(out); }
    return out.then(function(v){ restore(); return v; },
                    function(e){ restore();
                                 showError(String((e && e.message) || e));
                                 return null; });
  }

  /* -------------------------------------------------------- error strip */
  function showError(message, path){
    var host = byId('board-errors');
    if(!host){ console.error('board:', message); return; }
    var div = document.createElement('div');
    div.className = 'board-error';
    div.innerHTML = '<button type="button">dismiss</button>' + esc(message)
      + (path ? '<span class="path">' + esc(path) + '</span>' : '');
    div.querySelector('button').addEventListener('click', function(){
      if(div.parentNode) div.parentNode.removeChild(div);
    });
    host.appendChild(div);
  }
  function reportIfFailed(res){
    if(res && !res.ok) showError(res.error, res.path);
    return res;
  }

  /* ------------------------------------------------------------- banner */
  function setMode(next){
    if(mode === next) return;
    mode = next;
    var cl = document.body.classList;
    cl.toggle('board-live', next === 'live');
    cl.toggle('board-down', next === 'down');
    cl.toggle('board-file', next === 'file');
    var dead = (next !== 'live');
    var nodes = document.querySelectorAll('[data-board-control]');
    for(var i = 0; i < nodes.length; i++){ nodes[i].disabled = dead; }
    renderBanner();
  }
  function renderBanner(){
    var el = byId('board-banner');
    if(!el) return;
    el.className = '';
    if(mode === 'live'){ el.innerHTML = ''; return; }
    if(mode === 'file'){
      el.className = 'show readonly';
      el.innerHTML = '<b>Read-only \\u2014 opened as a file</b>'
        + 'This report is complete and readable, but interactive decisions '
        + 'need the loopback cockpit: the browser blocks '
        + 'requests from a <code>file://</code> page.'
        + '<div class="how">Open '
        + (HINT ? HINT_HTML : 'the board URL')
        + ' instead. If nothing is listening, run '
        + '<code>kit serve</code>, which refreshes both views, starts the cockpit, '
        + 'and prints the exact review URL.</div>';
      return;
    }
    el.className = 'show down';
    el.innerHTML = '<b>The cockpit is not reachable</b>'
      + 'Controls on this page are disabled because nothing answered at '
      + '<code>' + esc(boardUrl()) + '</code>. The page you are reading may have '
      + 'outlived the cockpit process that served it.'
      + '<div class="how">Bring it back with '
      + '<code>kit serve</code>, then press Retry.</div>'
      + '<button type="button" id="board-retry">Retry now</button>';
    var btn = byId('board-retry');
    if(btn) btn.addEventListener('click', function(){
      btn.disabled = true;
      btn.textContent = 'checking\\u2026';
      backoff = POLL_MS;
      probe().then(function(){
        // renderBanner() may have replaced this node already; only touch it
        // if it is still the one on the page.
        if(btn.parentNode){ btn.disabled = false; btn.textContent = 'Retry now'; }
      });
    });
  }

  /* -------------------------------------------------------------- state */
  function onState(fn){ stateSubs.push(fn); if(lastState) fn(lastState); }
  function emit(state){
    lastState = state;
    for(var i = 0; i < stateSubs.length; i++){
      try { stateSubs[i](state); } catch(e){ console.error('board: subscriber failed', e); }
    }
  }

  function probe(){
    if(isFile){ setMode('file'); return Promise.resolve(false); }
    return request('/api/health').then(function(h){
      if(!h.ok || !h.data || h.data.ok !== true
         || h.data.schema !== EXPECTED_SCHEMA || h.data.version !== EXPECTED_VERSION){
        setMode('down');
        if(h.ok && h.data && (h.data.schema !== EXPECTED_SCHEMA
                              || h.data.version !== EXPECTED_VERSION)){
          showError('This page and board use different kit versions. Restart with kit serve.');
        }
        return false;
      }
      setMode('live');
      return request('/api/state').then(function(s){
        if(!s.ok){ showError(s.error, s.path); return true; }
        emit(s.data || {});
        return true;
      });
    });
  }

  function schedule(){
    if(timer) clearTimeout(timer);
    if(isFile) return;
    timer = setTimeout(tick, backoff);
  }
  function tick(){
    if(document.hidden){ schedule(); return; }   // no polling behind a hidden tab
    return probe().then(function(alive){
      backoff = alive ? POLL_MS : Math.min(backoff * 2, POLL_MAX_MS);
      schedule();
      return alive;
    });
  }
  document.addEventListener('visibilitychange', function(){
    if(!document.hidden){ backoff = POLL_MS; tick(); }
  });

  function refresh(){ backoff = POLL_MS; return probe(); }

  function fmtMinutes(m){
    m = Number(m) || 0;
    if(m < 1) return 'under a minute';
    if(m < 60) return Math.round(m) + ' min';
    var h = Math.floor(m/60);
    return h + 'h ' + Math.round(m - h*60) + 'm';
  }
  function fmtElapsed(s){
    s = Math.round(Number(s) || 0);
    var m = Math.floor(s/60), r = s % 60;
    return m ? (m + 'm ' + r + 's') : (r + 's');
  }

  /* ------------------------------------------------- run status strip */
  function labelClass(status){
    status = String(status || '');
    if(status === 'completed' || status === 'finished') return 'finished';
    if(status === 'failed' || status === 'blocked' || status === 'unverified') return 'failed';
    return 'running';
  }
  function renderRuns(runs){
    var el = byId('board-status');
    if(!el) return;
    if(!runs || !runs.length){ el.innerHTML = '<i>no agent runs yet</i>'; return; }
    el.innerHTML = runs.map(function(r){
      return '<div class="run">'
        + (r.queue_total ? '<span>' + esc(r.queue_position) + ' of ' + esc(r.queue_total) + '</span>' : '')
        + '<span class="label ' + labelClass(r.status) + '">'
        + esc(r.status_label || r.status) + '</span>'
        + '<span>' + esc(r.persona || 'kit-builder')
        + (r.finding ? ' \\u2014 ' + esc(r.finding) : '') + '</span>'
        + (r.elapsed_s != null ? '<span>' + esc(fmtElapsed(r.elapsed_s)) + '</span>' : '')
        + (r.resume_cmd ? '<code>' + esc(r.resume_cmd) + '</code>'
                        : '<i>session id not resolved yet</i>')
        + (r.last_line ? '<span class="lastline">' + esc(r.last_line) + '</span>' : '')
        + '</div>';
    }).join('');
  }

  function start(){
    function ready(){ document.body.setAttribute('data-board-ready', 'true'); }
    if(isFile){ setMode('file'); ready(); return; }
    setMode('down');            // pessimistic until /api/health says otherwise
    probe().then(function(alive){
      backoff = alive ? POLL_MS : Math.min(backoff * 2, POLL_MAX_MS);
      schedule();
      ready();
    });
  }
  if(document.readyState === 'loading'){
    document.addEventListener('DOMContentLoaded', start);
  } else { start(); }

  onState(function(state){ renderRuns(state.runs); });

  return {esc:esc, request:request, guard:guard, showError:showError,
          reportIfFailed:reportIfFailed, onState:onState, refresh:refresh,
          fmtMinutes:fmtMinutes, fmtElapsed:fmtElapsed, boardUrl:boardUrl,
          isFile:function(){ return isFile; }, mode:function(){ return mode; }};
})();
</script>
""".replace("__HINT__", hint).replace("__HINT_HTML__", hint_html).replace(
        "__SCHEMA__", str(PROTOCOL_SCHEMA)
    ).replace("__VERSION__", version).replace("__POLL__", str(_POLL_MS)).replace(
        "__POLLMAX__", str(_POLL_MAX_MS)
    )


def shell_html() -> str:
    """The banner and error strip every page needs, in the order they read."""
    return '<div id="board-banner"></div>\n<div id="board-errors"></div>'


# Backwards-compatible name: plan.html previously embedded `JS` directly.
JS = core_js()
