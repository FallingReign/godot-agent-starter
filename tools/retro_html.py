#!/usr/bin/env python3
"""Render ranked retrospective findings as one HTML page for a human to read.

GENERATED, never authored, for the same reason plan.html is generated rather
than hand-written: the retrospective agent writes markdown facts, and letting
it also hand-author HTML would make it a second source of truth for the same
content, drifting from the markdown the moment either one changes without the
other. This tool reads what `tools/retro_rank.py` has already scored and
persisted -- it never rescoring a finding itself, so the number on this page
and the number in the findings file can never disagree.

    python tools/retro_html.py             # write retro.html
    python tools/retro_html.py --stdout

The page holds nothing of its own beyond what docs/retro/*-findings.md,
docs/retro/deferred.json and docs/retro/accepted.json already say, so
overwriting it is free, exactly like plan.html.

Two numbers that were previously invisible on the ranked list are shown here:

  - the RAW cost behind a stale-evidence discount, next to the discounted
    one, with the reason -- a finding scoring low because it may already be
    fixed reads very differently from one nobody cared about.
  - Observations -- findings with zero attributed human turns, with the
    reason they are not ranked, rather than only the ranked list.

Accept/defer decisions render as tick boxes, modeled on how plan.html renders
proposal.json's acknowledged[] (an unticked box is a decision to be made,
a ticked one shows who decided and why) -- see plan.html's "No design behind
this slice" banner for the same pattern. Decisions are never written into the
findings markdown itself: retro_rank.py rewrites that file from evidence on
every run, so anything stored only there would be silently discarded the next
time it runs. They live in docs/retro/accepted.json (mirroring the existing
docs/retro/deferred.json) instead.
"""
from __future__ import annotations

import argparse
import functools
import json
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import retro_rank  # noqa: E402  (sibling module, needs sys.path set first)
import plan_html  # noqa: E402  (reuse CSS/esc rather than a second visual language)
import board_client  # noqa: E402  (CSS/JS for the board status strip and interactive forms)
import retro_queue  # noqa: E402  (the prompt artifacts -- never re-derive a prompt here)
import md  # noqa: E402  (renders problem/proposal/measure and verbatim quotes -- no second renderer)

ROOT = retro_rank.ROOT
RETRO_DIR = retro_rank.RETRO_DIR
ACCEPTED_FILE = RETRO_DIR / "accepted.json"
OUT = ROOT / "retro.html"

esc = plan_html.esc

RAW_COST_RE = re.compile(r"raw cost ([\d.]+)")


# -------------------------------------------------------------- accepted.json
#
# Symmetric with retro_rank.load_deferred/save_deferred, kept as its own file
# rather than folded into deferred.json: accept and defer are different
# decisions with different futures (a deferral can recur and be boosted, an
# acceptance can be confirmed resolved), and a single file recording both
# would need a "kind" field the moment the two divergeed -- two small files
# with one shape each is simpler than one file with a discriminator.

def load_accepted() -> list[dict]:
    if not ACCEPTED_FILE.exists():
        return []
    try:
        return json.loads(ACCEPTED_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def save_accepted(entries: list[dict]) -> None:
    RETRO_DIR.mkdir(parents=True, exist_ok=True)
    ACCEPTED_FILE.write_text(json.dumps(entries, indent=2) + "\n", encoding="utf-8")


def mark_resolved_if_absent(entries: list[dict], present_titles: set[str]) -> bool:
    """An accepted finding absent from current evidence is a confirmed win.

    Mirrors retro_rank.apply_deferred_votes' resolved_at bookkeeping so
    acceptance is judged by the same rule deferral already is: nothing to
    fix a second time counts as fixed.
    """
    today = date.today().isoformat()
    changed = False
    for e in entries:
        key = retro_rank.normalise_title(e.get("finding", ""))
        if key not in present_titles and not e.get("resolved_at") and e.get("date", today) < today:
            e["resolved_at"] = today
            changed = True
    return changed


# ------------------------------------------------------------------- ordering
#
# One rule, stated once, applied twice: here when the page is generated, and
# again in DISPATCH_JS when the board re-partitions the same cards live. Both
# implementations are pure functions of the row data -- no dict order, no JSON
# insertion order, no DOM order -- so the same input always renders the same
# page. ORDER_CAPTIONS is what the page tells the human the rule is, so the
# caption cannot drift from the sort.
#
# A row is {"title", "cost", "state", "updated_at"}; `state` is "" when only
# the on-disk decision is known and the board has not spoken.

# working/stalled first because live activity is what the human must see
# immediately; failed last because it is over. Anything unrecognised sorts
# after all of them rather than silently landing at the top.
APPROVED_STATE_ORDER = {"working": 0, "stalled": 0, "queued": 1, "done": 2, "failed": 3}
UNKNOWN_STATE_RANK = 4

ORDER_CAPTIONS = {
    "toaction": "Ordered by cost, highest first. Findings that could not be ranked come last.",
    "approved": "Ordered working or stalled first, then queued, then done, then failed; "
                "most recently updated first within each.",
    "deferred": "Ordered most recently deferred first.",
}


def _cmp(a: object, b: object) -> int:
    return (a > b) - (a < b)  # type: ignore[operator]


def _cmp_to_action(a: dict, b: dict) -> int:
    ac, bc = a.get("cost"), b.get("cost")
    r = _cmp(ac is None, bc is None)          # unranked last
    if r:
        return r
    if ac is not None and bc is not None:
        r = _cmp(bc, ac)                       # cost descending
        if r:
            return r
    return _cmp(a.get("title", ""), b.get("title", ""))


def _cmp_approved(a: dict, b: dict) -> int:
    r = _cmp(APPROVED_STATE_ORDER.get(a.get("state") or "", UNKNOWN_STATE_RANK),
             APPROVED_STATE_ORDER.get(b.get("state") or "", UNKNOWN_STATE_RANK))
    if r:
        return r
    r = _cmp(b.get("updated_at") or "", a.get("updated_at") or "")   # newest first
    if r:
        return r
    return _cmp(a.get("title", ""), b.get("title", ""))


def _cmp_deferred(a: dict, b: dict) -> int:
    r = _cmp(b.get("updated_at") or "", a.get("updated_at") or "")
    if r:
        return r
    return _cmp(a.get("title", ""), b.get("title", ""))


def order_rows(kind: str, rows: list[dict]) -> list[dict]:
    """Sort rows for one list. Pure, total, and stable under shuffling.

    Every comparator ends on `title`, which is unique per finding, so no two
    rows ever compare equal -- the output cannot depend on the input order.
    """
    cmp = {"toaction": _cmp_to_action,
           "approved": _cmp_approved,
           "deferred": _cmp_deferred}[kind]
    return sorted(rows, key=functools.cmp_to_key(cmp))


# ------------------------------------------------------------------- render

CSS_EXTRA = """
/* ---- tabs (approved mock): underline, not pills -- plan.html has no
   button-heavy chrome and a pill row would read as a second toolbar. ---- */
.tabs{display:flex;gap:2px;border-bottom:1px solid var(--line);margin:0 0 18px}
.tab{appearance:none;background:none;border:0;border-bottom:2px solid transparent;
color:var(--dim);font:inherit;font-weight:600;padding:9px 15px 8px;cursor:pointer;
display:flex;align-items:center;gap:7px;border-radius:5px 5px 0 0}
.tab:hover{color:var(--fg);background:#1b1f2a}
.tab[aria-selected="true"]{color:var(--fg);border-bottom-color:var(--acc)}
.tab .n{font-size:11px;font-weight:700;color:var(--dim);background:#1b1f2a;
border:1px solid var(--line);border-radius:9px;padding:0 6px;min-width:19px;text-align:center}
.tab[aria-selected="true"] .n{color:var(--bg);background:var(--acc);border-color:var(--acc)}
.tab.alarm .n{color:#fff;background:var(--bad);border-color:var(--bad)}
.panel[hidden]{display:none}

/* Above the tabs, not inside a panel, so the alarm survives being on
   another tab. */
.banner{background:#1a1206;border:1px solid #5b4410;color:var(--warn);
border-radius:7px;padding:9px 13px;font-size:13px;margin:0 0 16px}
.banner[hidden]{display:none}

.finding{background:var(--card);border:1px solid var(--line);border-radius:8px;
padding:16px 18px 15px;margin:0 0 11px;display:grid;
grid-template-columns:66px minmax(0,1fr);column-gap:16px}
.finding .gutter{border-right:1px solid var(--line);padding-right:14px;text-align:right}
.finding .rank{display:block;color:#5c6473;font-size:11px;font-weight:700;letter-spacing:.06em}
.finding .cost{display:block;font-size:21px;font-weight:700;color:var(--acc);
line-height:1.15;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.finding .raw{display:block;font-size:12px;font-weight:400;color:var(--dim);
font-style:italic;margin:0 0 6px}
.finding .effort{display:block;font-size:11px;color:var(--dim);margin-top:1px}
.finding .qw{font-size:10.5px;text-transform:uppercase;letter-spacing:.06em;
padding:1.5px 7px;border-radius:9px;border:1px solid #1f4d2b;color:var(--ok);margin-left:8px}
.finding .sev{font-size:10.5px;text-transform:uppercase;letter-spacing:.06em;
padding:1.5px 7px;border-radius:9px;border:1px solid var(--line);color:var(--dim);margin-left:6px}
.finding .sev.bad{color:var(--bad);border-color:#5c2224}
.finding h3.title{margin:0 0 4px;font-size:15px;letter-spacing:-.01em;line-height:1.35}
.finding .badges{margin:0 0 6px}
.finding .section{margin:8px 0}
.finding .section .label{font-size:11px;text-transform:uppercase;letter-spacing:.06em;
color:var(--dim);margin-bottom:2px}
.finding .section .body p{margin:2px 0}
.finding .facts{color:var(--dim);font-size:12.5px;margin:8px 0 0}
.finding details.evidence{margin:8px 0 0;padding:4px 12px;background:#0d1117;
border-radius:6px;border:1px solid var(--line)}
.finding details.evidence summary{cursor:pointer;padding:6px 0;color:var(--dim);font-size:12.5px}
.finding .quote{margin:3px 0;font-size:13px;color:#e8edf3}
.finding .quote .cite{color:var(--dim);font-size:11.5px}
.finding .mech-summary{margin:3px 0;font-size:12.5px;color:var(--dim)}
.finding .note{color:#8a7a52;font-size:12px;margin:3px 0}
.finding .decision{margin-top:13px;padding-top:13px;border-top:1px solid var(--line)}
.decision-form input,.decision-form textarea{background:#0c0e13;color:var(--fg);
border:1px solid var(--line);border-radius:6px;padding:8px 11px;font-size:13px;
font-family:inherit;box-sizing:border-box}
.decision-form textarea{width:100%;min-height:44px;display:block;margin:0 0 2px;resize:vertical}
.decision-form textarea:focus,.decision-form input:focus{outline:none;border-color:var(--acc)}

/* Approving dispatches (decision 4). The primary action uses the accent
   blue, not amber -- amber is reserved for the warning line below the
   button row, which is where "no second confirmation" belongs. */
.btn{appearance:none;border-radius:6px;font:inherit;font-weight:600;cursor:pointer;
padding:7px 14px;border:1px solid;font-size:13px}
.approve-btn{background:var(--acc);color:#08111d;border-color:var(--acc);
font-weight:700;font-size:13.5px;line-height:1.25;padding:9px 16px;border-radius:6px;
cursor:pointer;white-space:normal;max-width:100%;display:inline-block;box-sizing:border-box}
.approve-btn:hover{filter:brightness(1.1)}
.approve-btn[disabled]{background:#2a4a6b;color:#9db6cc}
.row{display:flex;gap:10px;align-items:center;margin-top:10px;flex-wrap:wrap}
.defer-toggle-btn{background:transparent;color:var(--dim);border:1px solid var(--line);
border-radius:6px;padding:7px 14px;font-size:13px;cursor:pointer;margin-left:auto}
.defer-toggle-btn:hover{color:var(--fg);border-color:#39414f}
.defer-toggle-btn[hidden]{display:none}
.quota-warn{display:block;margin:9px 0 0;font-size:12px;color:#d9a441}
.quota-warn b{color:#ffcf8a}
.deferbox{display:flex;gap:8px;margin-top:11px}
.deferbox[hidden]{display:none}
.deferbox input{flex:1}
.defer-confirm-btn,.defer-cancel-btn,.move-back-btn{background:transparent;color:var(--dim);
border:1px solid var(--line);border-radius:6px;padding:7px 14px;font-size:13px;cursor:pointer}
.defer-confirm-btn:hover,.defer-cancel-btn:hover,.move-back-btn:hover{color:var(--fg);
border-color:#39414f}

.prompt-block{margin:2px 0 0;padding:0;background:none;border:0}
.prompt-block summary{cursor:pointer;padding:3px 0;color:var(--acc);font-size:12.5px;
list-style:none;display:flex;align-items:center;gap:6px;width:fit-content}
.prompt-block summary:hover{text-decoration:underline}
.prompt-block summary::-webkit-details-marker{display:none}
.prompt-block summary::before{content:"\u25b8";color:#5c6473;font-size:9px}
.prompt-block[open]>summary::before{content:"\u25be"}
.prompt-block pre{white-space:pre-wrap;word-break:break-word;font-size:12px;
max-height:340px;overflow:auto;margin:5px 0 9px;color:#e8edf3;padding:11px 13px;
background:#0c0e13;border:1px solid var(--line);border-radius:6px}
.prompt-block .src{color:var(--dim);font-size:11.5px;margin:2px 0 6px}
.prompt-block .stale{color:var(--bad)}

.list-note{color:var(--dim);font-size:13px;margin:0 0 6px}
.list-caption{color:var(--dim);font-size:12px;margin:0 0 14px;font-style:italic}
.list-empty{color:var(--dim);font-style:italic;margin:0 0 12px}
.unranked-note{color:#d9a441;font-size:12.5px;margin:6px 0 0}
.finding details.detail-block{margin:2px 0 0;padding:0;background:none;border:0}
.finding details.detail-block>summary{cursor:pointer;padding:3px 0;color:var(--acc);
font-size:12.5px;list-style:none;display:flex;align-items:center;gap:6px;width:fit-content}
.finding details.detail-block>summary:hover{text-decoration:underline}
.finding details.detail-block>summary::-webkit-details-marker{display:none}
.finding details.detail-block>summary::before{content:"\u25b8";color:#5c6473;font-size:9px}
.finding details.detail-block[open]>summary::before{content:"\u25be"}
.finding .teaser{color:#c2ccd6;font-size:13px;margin:4px 0 0}

/* Per-item status on an approved finding. The whole point of this block is
   that an item wedged on `working` must not look like normal progress, so a
   stalled item gets a colour, a border and a stated instruction rather than a
   timestamp the human has to do arithmetic on. */
.status-slot{margin:10px 0 0;padding:8px 12px;border-radius:6px;
border:1px solid var(--line);background:#0d1117;font-size:12.5px}
.status-slot .pill{display:inline-block;font-weight:700;font-size:10.5px;
text-transform:uppercase;letter-spacing:.06em;padding:2px 8px;border-radius:9px;
border:1px solid var(--line);margin-right:8px}
.status-slot .detail{color:var(--dim);margin-top:5px}
.status-slot code{background:#151b24;padding:1px 6px;border-radius:4px;user-select:all}
.status-slot.queued .pill{color:var(--dim)}
.status-slot.working{border-color:#2a5a8f}
.status-slot.working .pill{color:var(--acc);border-color:#2a5a8f}
.status-slot.done{border-color:#1f4d2b}
.status-slot.done .pill{color:var(--ok);border-color:#1f4d2b}
.status-slot.failed,.status-slot.stalled{border-color:var(--bad);background:#241416}
.status-slot.failed .pill,.status-slot.stalled .pill{color:#fff;background:var(--bad);
border-color:var(--bad)}
.status-slot.stalled{animation:stall-pulse 1.6s ease-in-out infinite}
.status-slot.stalled .detail{color:#f0c9c9}
@keyframes stall-pulse{0%,100%{box-shadow:0 0 0 0 rgba(200,60,60,.55)}
50%{box-shadow:0 0 0 5px rgba(200,60,60,0)}}
.status-slot .amendment{margin-top:6px;padding-left:9px;border-left:2px solid var(--line);
color:#e8edf3}
.finding.is-stale{border-color:#8a5a1f}
.stale-flag{color:#d9a441;font-size:12px;margin:6px 0 0}
"""

# How long a `working` item may be silent before the page calls it stalled
# rather than progressing. The board reports the number; this decides at what
# point the page stops presenting it as normal. Five minutes is well past any
# single tool call a kit-builder makes and well short of a real build.
STALL_MINUTES = 5

# Every board call on this page goes through Board.request/Board.guard, so
# every one of them checks response.ok, survives a non-JSON body, shows a
# readable error, and restores its control however it ends. There is no
# fetch() in this file (review finding 4).
DISPATCH_JS = """
<script>
(function(){
  var B = window.Board;
  var STALL = __STALL__;

  /* ------------------------------------------------ exact prompt preview
     Decision 4 dispatches on approval, so the human must be able to read
     the prompt BEFORE approving. The card ships with the on-disk artifact
     prompt inlined (so file:// can read it too); opening the disclosure on
     a live board replaces it with prompt_preview from the board, which is
     render_prompt(item, "") -- the same function the spawn path calls. */
  function wirePromptPreview(det){
    /* A settled finding's prompt is history: it was already sent (or, for a
       deferral, deliberately not sent). Refetching the live preview here
       would overwrite the past-tense caption with "will be sent" and make a
       dispatched item read as one still awaiting a decision. */
    if(det.getAttribute('data-settled')) return;
    var slug = det.getAttribute('data-slug');
    var loaded = false;
    det.addEventListener('toggle', function(){
      if(!det.open || loaded || B.isFile()) return;
      loaded = true;
      var pre = det.querySelector('pre');
      var src = det.querySelector('.src');
      B.request('/api/finding/' + encodeURIComponent(slug)).then(function(res){
        if(!res.ok){
          loaded = false;                       // let the next open retry
          B.showError(res.error, res.path);
          if(src) src.innerHTML = 'Showing the on-disk artifact: the board could '
                                + 'not be asked for the live preview.';
          return;
        }
        var d = res.data || {};
        if(typeof d.prompt_preview === 'string') pre.textContent = d.prompt_preview;
        if(src) src.innerHTML = 'This is the exact text that will be sent, from the '
          + 'board. Your comment is appended below it.'
          + (d.stale ? '<span class="stale"> The findings file has changed since '
                     + 'this prompt was built \\u2014 it is stale. Re-run '
                     + '<code>python tools/retro_queue.py</code>.</span>' : '');
      });
    });
  }

  /* ----------------------------------------------------------- decisions */
  function wireDecision(box){
    var slug = box.getAttribute('data-slug');
    var comment = box.querySelector('.comment');
    var approve = box.querySelector('.approve-btn');
    var toggle = box.querySelector('[data-defer]');
    var reason = box.querySelector('.defer-reason');
    var confirmDefer = box.querySelector('.defer-confirm-btn');
    var cancel = box.querySelector('[data-cancel]');
    var all = [comment, approve, toggle, reason, confirmDefer, cancel];

    if(approve) approve.addEventListener('click', function(){
      B.guard(all, 'dispatching\\u2026', function(){
        return B.request('/api/finding/approve', {method:'POST',
          body:{slug:slug, comment:(comment ? comment.value : '')}})
          .then(function(res){
            if(!res.ok){ B.showError(res.error, res.path); return; }
            B.refresh();
          });
      });
    });

    if(confirmDefer) confirmDefer.addEventListener('click', function(){
      B.guard(all, 'deferring\\u2026', function(){
        return B.request('/api/finding/defer', {method:'POST',
          body:{slug:slug, reason:(reason ? reason.value : '')}})
          .then(function(res){
            if(!res.ok){ B.showError(res.error, res.path); return; }
            B.refresh();
          });
      });
    });
  }

  /* "Defer instead" starts hidden behind the button and reveals its own
     single input -- only one text field is ever visible per card. Cancel
     restores the button and clears the text. */
  function wireDeferToggle(box){
    var toggle = box.querySelector('[data-defer]');
    var deferbox = box.querySelector('.deferbox');
    var cancel = box.querySelector('[data-cancel]');
    if(!toggle || !deferbox) return;
    toggle.addEventListener('click', function(){
      deferbox.hidden = false;
      toggle.hidden = true;
      var inp = deferbox.querySelector('input');
      if(inp) inp.focus();
    });
    if(cancel) cancel.addEventListener('click', function(){
      deferbox.hidden = true;
      var inp = deferbox.querySelector('input');
      if(inp) inp.value = '';
      toggle.hidden = false;
    });
  }

  /* --------------------------------------------------------------- tabs
     Underline tabs, one panel visible at a time. The wedged banner lives
     above the tabs (wired in apply(), below) so the alarm survives being on
     another tab. */
  function wireTabs(){
    var tabs = [].slice.call(document.querySelectorAll('.tab'));
    tabs.forEach(function(t){
      t.addEventListener('click', function(){
        tabs.forEach(function(o){
          var on = o === t;
          o.setAttribute('aria-selected', on ? 'true' : 'false');
          var p = document.getElementById(o.getAttribute('data-p'));
          if(p) p.hidden = !on;
        });
      });
    });
  }

  /* ------------------------------------------------------ status of one item
     A wedged item must be obvious. `working` plus silence past STALL minutes
     is rendered as its own state -- red, pulsing, and carrying the sentence
     that says what to do -- rather than as a timestamp to subtract. */
  function statusHtml(f, run){
    var state = f.state || 'awaiting_review';
    var silent = run ? Number(run.silence_minutes || 0) : 0;
    var stalled = (state === 'working' && silent >= STALL);
    var cls = stalled ? 'stalled' : state;
    var pill = state.replace(/_/g, ' ');
    var head = '';

    if(stalled){
      pill = 'stalled \\u2014 silent ' + B.fmtMinutes(silent);
      head = 'This worker has produced no output for ' + B.esc(B.fmtMinutes(silent))
           + '. It is not progressing on its own \\u2014 open the chat and look.';
    } else if(state === 'working'){
      head = 'Working' + (run && run.silence_minutes != null
        ? ' \\u2014 last output ' + B.esc(B.fmtMinutes(silent)) + ' ago.' : '.');
    } else if(state === 'queued'){
      head = 'Queued behind a running worker. One finding runs at a time.';
    } else if(state === 'failed'){
      head = 'The worker exited non-zero'
           + (run && run.exit_code != null ? ' (exit ' + B.esc(run.exit_code) + ')' : '')
           + '. The queue is halted until this is dealt with.';
    } else if(state === 'done'){
      head = 'Finished.';
    } else if(state === 'deferred'){
      head = 'Deferred. Nothing was dispatched.';
    }

    var html = '<div class="status-slot ' + B.esc(cls) + '">'
      + '<span class="pill">' + B.esc(pill) + '</span>' + head;
    if(f.status_detail) html += '<div class="detail">' + B.esc(f.status_detail) + '</div>';
    if(f.comment){
      html += '<div class="detail">Approved with this comment, which was appended '
            + 'to the prompt:</div><div class="amendment">' + B.esc(f.comment) + '</div>';
    }
    if(run && run.resume_cmd){
      html += '<div class="detail">Open the worker\\u2019s chat: <code>'
            + B.esc(run.resume_cmd) + '</code></div>';
    } else if(state === 'working'){
      html += '<div class="detail">Session id not resolved yet, so there is no '
            + 'resume command to show.</div>';
    }
    html += '</div>';
    return html;
  }

  /* ---------------------------------------------------------------- state
     The board's view of a card can differ from the decision baked into the
     page, so placement is recomputed here from both -- and then each list is
     sorted by the SAME rule retro_html.order_rows() used when the page was
     generated (retro_html.APPROVED_STATE_ORDER / _cmp_*). Pure function of
     the row data: no DOM order, no findings[] order, no dict order. */
  var STATE_ORDER = {working:0, stalled:0, queued:1, done:2, failed:3};
  var UNKNOWN_RANK = 4;

  function cmp(a, b){ return a > b ? 1 : (a < b ? -1 : 0); }

  function cmpToAction(a, b){
    var r = cmp(a.cost === null, b.cost === null);          // unranked last
    if(r) return r;
    if(a.cost !== null && b.cost !== null){
      r = cmp(b.cost, a.cost);                              // cost descending
      if(r) return r;
    }
    return cmp(a.title, b.title);
  }
  function cmpApproved(a, b){
    var ra = STATE_ORDER[a.state] === undefined ? UNKNOWN_RANK : STATE_ORDER[a.state];
    var rb = STATE_ORDER[b.state] === undefined ? UNKNOWN_RANK : STATE_ORDER[b.state];
    var r = cmp(ra, rb);
    if(r) return r;
    r = cmp(b.updated, a.updated);                          // newest first
    if(r) return r;
    return cmp(a.title, b.title);
  }
  function cmpDeferred(a, b){
    var r = cmp(b.updated, a.updated);
    if(r) return r;
    return cmp(a.title, b.title);
  }
  var COMPARATORS = {toaction:cmpToAction, approved:cmpApproved, deferred:cmpDeferred};

  /* Which list a card belongs in. A recorded approval or deferral is settled
     whatever the board says, so an approved card can never be emitted into
     #list-toaction; the board state only refines the ordering within the
     settled lists (and can settle a card the page was generated without).
     A recorded deferral wins outright: nothing was dispatched for it, so no
     run state can promote it back into work. */
  function bucketOf(decided, state, stalled){
    if(decided === 'deferred' || state === 'deferred') return 'deferred';
    if(stalled || STATE_ORDER[state] !== undefined || decided === 'approved') return 'approved';
    return 'toaction';
  }

  function rowFor(card, f, run){
    var costAttr = card.getAttribute('data-cost');
    var state = (f && f.state && f.state !== 'awaiting_review') ? f.state : '';
    var silent = run ? Number(run.silence_minutes || 0) : 0;
    var stalled = (state === 'working' && silent >= STALL);
    return {
      card: card,
      cost: costAttr === '' || costAttr === null ? null : Number(costAttr),
      title: card.getAttribute('data-title') || '',
      decided: card.getAttribute('data-decided') || '',
      state: stalled ? 'stalled' : state,
      updated: (f && f.updated_at) || card.getAttribute('data-updated') || '',
      finding: f,
      run: run,
      stalled: stalled
    };
  }

  function apply(state){
    var findings = (state && state.findings) || [];
    var runs = (state && state.runs) || [];
    var byRun = {}, bySlug = {};
    runs.forEach(function(r){ if(r && r.run_id) byRun[r.run_id] = r; });
    findings.forEach(function(f){ if(f && f.slug) bySlug[f.slug] = f; });

    var hosts = {toaction: document.getElementById('list-toaction'),
                 approved: document.getElementById('list-approved'),
                 deferred: document.getElementById('list-deferred')};
    if(!hosts.toaction || !hosts.approved || !hosts.deferred) return;

    var buckets = {toaction:[], approved:[], deferred:[]};
    var cards = document.querySelectorAll('.finding[data-slug]');
    for(var i = 0; i < cards.length; i++){
      var card = cards[i];
      var f = bySlug[card.getAttribute('data-slug')] || null;
      var row = rowFor(card, f, f ? byRun[f.run_id] : null);
      var slot = card.querySelector('.status-host');
      if(slot) slot.innerHTML = (f && f.state && f.state !== 'awaiting_review')
        ? statusHtml(f, row.run) : '';
      card.classList.toggle('is-stale', !!(f && f.stale));
      var settled = bucketOf(row.decided, row.state, row.stalled) !== 'toaction';
      var form = card.querySelector('.decision-form');
      if(form) form.style.display = settled ? 'none' : '';
      // "Awaiting review" on an item the board is already running is a lie the
      // human has to reconcile; hide the whole unticked block, not just its
      // controls. A recorded decision has its own block and always shows.
      var awaiting = card.querySelector('.decision.awaiting');
      if(awaiting) awaiting.style.display = settled ? 'none' : '';
      buckets[bucketOf(row.decided, row.state, row.stalled)].push(row);
    }

    var stalledCount = 0;
    // Moving a node blurs anything focused inside it, so a poll that reorders
    // while the human is mid-sentence eats the caret. The order still has to
    // update, so capture the selection, let the reorder happen, then put it
    // back -- rather than skipping the reorder and letting the list lie.
    var active = document.activeElement;
    var restore = null;
    if(active && (active.tagName === 'TEXTAREA' || active.tagName === 'INPUT')){
      restore = {el: active, start: active.selectionStart, end: active.selectionEnd};
    }
    ['toaction','approved','deferred'].forEach(function(kind){
      var host = hosts[kind];
      buckets[kind].sort(COMPARATORS[kind]);
      buckets[kind].forEach(function(row, idx){
        host.appendChild(row.card);                 // reappend == reorder
        var rank = row.card.querySelector('.rank');
        if(rank) rank.textContent = (idx + 1) + '.';
        if(kind === 'approved' && row.stalled) stalledCount++;
      });
      var empty = host.querySelector('.list-empty');
      if(empty) empty.style.display = buckets[kind].length ? 'none' : '';
      var count = document.querySelector('[data-count-for="' + kind + '"]');
      if(count) count.textContent = buckets[kind].length;
    });

    // Put the caret back where the human left it. Guarded on the node still
    // being in the document: a card that moved to another list keeps its
    // textarea, but one that was replaced outright must not steal focus.
    if(restore && restore.el && document.body
       && typeof document.body.contains === 'function'
       && document.body.contains(restore.el)
       && document.activeElement !== restore.el){
      try{
        restore.el.focus();
        if(restore.start !== null && restore.start !== undefined
           && typeof restore.el.setSelectionRange === 'function'){
          restore.el.setSelectionRange(restore.start, restore.end);
        }
      }catch(e){ /* a detached or read-only node is not worth throwing over */ }
    }

    // The wedge banner and the Activity tab's red badge: the alarm must be
    // visible from any tab, so this lives above the tabs, not in a panel.
    var banner = document.getElementById('wedge-banner');
    var activityTab = document.querySelector('.tab[data-p="panel-approved"]');
    if(banner){
      banner.hidden = stalledCount === 0;
      if(stalledCount){
        banner.innerHTML = '<b>' + stalledCount + ' worker'
          + (stalledCount === 1 ? ' is' : 's are') + ' wedged.</b> '
          + 'It has produced no output for a while. Open the Activity tab.';
      }
    }
    if(activityTab) activityTab.classList.toggle('alarm', stalledCount > 0);
  }

  document.querySelectorAll('.prompt-block').forEach(wirePromptPreview);
  document.querySelectorAll('.decision-form').forEach(wireDecision);
  document.querySelectorAll('.decision-form').forEach(wireDeferToggle);
  wireTabs();
  B.onState(apply);

  /* A per-finding comment thread would mount into .thread-slot on each card
     and subscribe through B.onState the same way this does. Deliberately not
     built yet -- the slot and the subscription shape are what keep it from
     needing a rewrite. */
})();
</script>
""".replace("__STALL__", str(STALL_MINUTES))


MARKER_RE = re.compile(r"^(LOOP|REWRITE|FAIL|PASS)\b")


def render_evidence(f: dict, digests: dict[str, dict]) -> str:
    """Evidence, collapsed: verbatim human-turn quotes plus mechanical
    markers summarised to one line per marker type with a total, rather than
    one row per occurrence -- the raw evidence stays on the page (a citation
    that does not support its finding is the only failure mode nobody else
    can catch), it is just not the most prominent thing on it any more.
    """
    quotes: list[str] = []
    for cite in f["human_turns"]:
        m = retro_rank.CITE_RE.fullmatch(cite.strip())
        text = ""
        if m and m.group(2):
            tag = f"S{m.group(1)}"
            hnum = int(m.group(2))
            d = digests.get(tag)
            if d and not d.get("error"):
                human = d.get("human", [])
                if 1 <= hnum <= len(human):
                    text = human[hnum - 1]
        cite_html = f'<span class="cite">{esc(cite)}</span>'
        if text:
            quotes.append(f'<div class="quote">{cite_html} &mdash; {md.render(text)}</div>')
        else:
            quotes.append(f'<div class="quote">{cite_html} &mdash; <i>text not in digest window</i></div>')

    marker_totals: dict[str, int] = {}
    marker_entries: dict[str, int] = {}
    marker_sessions: dict[str, set] = {}
    for entry in f["mechanical"]:
        m = MARKER_RE.match(entry.strip())
        marker = m.group(1) if m else "other"
        # Sum exactly the way retro_rank.score() does -- COUNT_RE.findall
        # over the raw entry text, no default of 1 for an entry with no
        # explicit "xN" -- so this total can never disagree with the number
        # that actually fed the cost.
        counts = [int(c) for c in retro_rank.COUNT_RE.findall(entry)]
        marker_totals[marker] = marker_totals.get(marker, 0) + sum(counts)
        marker_entries[marker] = marker_entries.get(marker, 0) + 1
        sess_m = retro_rank.MECH_SESSION_RE.search(entry)
        marker_sessions.setdefault(marker, set())
        if sess_m:
            marker_sessions[marker].add(sess_m.group(1))

    mech_lines = []
    for marker, total in sorted(marker_totals.items()):
        n_sess = len(marker_sessions.get(marker, set()))
        n_entries = marker_entries.get(marker, 0)
        sess_note = f" across {n_sess} session{'s' if n_sess != 1 else ''}" if n_sess else ""
        mech_lines.append(
            f'<div class="mech-summary">{esc(marker)} &times;{total} '
            f'({n_entries} entr{"y" if n_entries == 1 else "ies"}){esc(sess_note)}</div>'
        )

    if not quotes and not mech_lines:
        return ""
    summary = (f"Evidence &mdash; {len(quotes)} human turn(s)"
               + (f", {sum(marker_totals.values())} mechanical marker(s)" if marker_totals else ""))
    return (f'<details class="evidence"><summary>{summary}</summary>'
            + "".join(quotes) + "".join(mech_lines) + "</details>")


def render_section(label: str, text: str) -> str:
    if not text:
        return ""
    return (f'<div class="section"><div class="label">{esc(label)}</div>'
            f'<div class="body">{md.render(text)}</div></div>')


# Tense follows the decision, because the same block means three different
# things depending on it. A settled card offering "the prompt that WILL be
# sent -- read it before approving" invites a decision that has already been
# taken and cannot be taken again; on a deferral nothing was ever sent at all.
PROMPT_SUMMARY = {
    "": "The exact prompt that will be sent &mdash; read it before approving",
    "approved": "The exact prompt that was sent",
    "deferred": "The prompt that would have been sent &mdash; this finding was deferred, "
                "so nothing was dispatched",
}
PROMPT_COMMENT_NOTE = {
    "": "Your comment is appended below it.",
    "approved": "Your comment was appended below it.",
    "deferred": "Nothing was dispatched.",
}


def render_prompt_block(item: dict | None, decided: str = "") -> str:
    """The exact prompt, readable before approval (decision 4).

    The artifact's own prompt is inlined so a `file://` reader sees it too;
    `render_prompt(item, "")` is by construction identical to `item["prompt"]`,
    so the inline copy and the board's `prompt_preview` agree. On a live board
    the disclosure refetches anyway, because only the board can tell the human
    the artifact has gone stale -- but only while the decision is still open.
    """
    if not item:
        if decided:
            return '<div class="stale-flag">No dispatch artifact for this finding.</div>'
        return ('<div class="stale-flag">No dispatch artifact for this finding. Run '
                "<code>python tools/retro_queue.py</code> to build one; until then it "
                "cannot be approved.</div>")
    # Staleness is a warning about a decision still to be made. On a settled
    # finding the prompt shown is the record of what was (or was not) sent, so
    # there is nothing to re-run before doing anything.
    stale = ('<div class="stale-flag">The findings file has changed since this prompt was '
             "built, so it is stale. Re-run <code>python tools/retro_queue.py</code> before "
             "approving.</div>") if (retro_queue.is_stale(item) and not decided) else ""
    prompt = retro_queue.render_prompt(item, "")
    settled_attr = f' data-settled="{esc(decided)}"' if decided else ""
    return (
        stale
        + f'<details class="prompt-block" data-slug="{esc(item["slug"])}"{settled_attr}>'
        + f'<summary>{PROMPT_SUMMARY[decided]}</summary>'
        + '<div class="src">From the on-disk artifact '
        + f'<code>docs/retro/queue/{esc(item["slug"])}.json</code>, built '
        + f'{esc(item.get("generated_at", "") or "unknown")}. '
        + f'{PROMPT_COMMENT_NOTE[decided]}</div>'
        + f"<pre>{esc(prompt)}</pre></details>"
    )


def render_decision(title: str, item: dict | None,
                    accepted: dict[str, dict], deferred: dict[str, dict]) -> str:
    """Recorded decision, or the approve-with-comment control.

    One control, not two clicks: writing the comment is the conscious act and
    approving dispatches immediately (decision 4). The warning therefore lives
    on the button, because there is no confirmation step behind it.
    """
    key = retro_rank.normalise_title(title)
    acc = accepted.get(key)
    dfr = deferred.get(key)
    awaiting = not (acc or dfr)
    parts = ['<div class="decision awaiting">' if awaiting else '<div class="decision">']
    if acc:
        who = esc(acc.get("by", "") or "")
        when = esc(acc.get("date", "") or "")
        why = esc(acc.get("comment", "") or acc.get("reason", "") or "")
        parts.append("<b>&#9745; Approved</b>" + (f" by {who}" if who else "")
                     + (f" on {when}" if when else "") + ".")
        if why:
            parts.append(f'<div style="margin-top:4px">{why}</div>')
        if acc.get("resolved_at"):
            parts.append('<div class="note">confirmed win: absent from evidence as of '
                         f'{esc(acc["resolved_at"])}</div>')
    elif dfr:
        who = esc(dfr.get("by", "") or "")
        when = esc(dfr.get("date", "") or "")
        why = esc(dfr.get("reason", "") or "")
        parts.append("<b>&#9745; Deferred</b>" + (f" by {who}" if who else "")
                     + (f" on {when}" if when else "") + ".")
        if why:
            parts.append(f'<div style="margin-top:4px">{why}</div>')
        if dfr.get("recurred_at"):
            parts.append('<div class="note">recurred with new evidence since being deferred '
                         "-- score boosted</div>")
    else:
        parts.append("<b>&#9744; Awaiting review.</b>")
        if item:
            # Always rendered, never hidden: a control the human cannot see
            # cannot explain why it is unavailable. Board.setMode disables
            # every [data-board-control] when the board is not live, and the
            # banner above says which of the two reasons applies.
            # Only one text field is ever visible at once: the comment is
            # always there (it is the conscious decision behind approval),
            # but "Defer instead" starts hidden behind a quiet ghost button
            # at the right of the same row and only reveals its own single
            # input when clicked. Cancel restores the button and clears the
            # text -- an explicit correction from the human, not to regress.
            parts.append(
                f'<div class="decision-form" data-slug="{esc(item["slug"])}">'
                '<textarea class="comment" rows="3" data-board-control '
                'placeholder="Decisions or tweaks to the proposed solution. '
                'Leave empty to dispatch the proposal as written."></textarea>'
                '<div class="row">'
                '<button type="button" class="approve-btn" data-board-control>'
                "Approve &amp; dispatch now</button>"
                '<button type="button" class="defer-toggle-btn" data-board-control '
                'data-defer>Defer instead</button>'
                "</div>"
                '<span class="quota-warn"><b>Approving starts a kit-builder worker '
                "immediately and spends quota.</b> There is no second confirmation: this "
                "comment is the conscious decision. Whatever you write is appended to the "
                "prompt above and wins where it disagrees with the proposal.</span>"
                '<div class="deferbox" hidden>'
                '<input class="defer-reason" data-board-control '
                'placeholder="reason for deferring, in your words">'
                '<button type="button" class="defer-confirm-btn" data-board-control>'
                "Confirm defer</button>"
                '<button type="button" class="defer-cancel-btn" data-cancel>Cancel</button>'
                "</div></div>"
            )
    if dfr:
        # Deferred settled decisions are not just a record -- they carry a
        # (currently inert, matching the approved mock) control to reopen
        # them, so the archive does not read as a dead end.
        parts.append('<div class="row"><button type="button" class="move-back-btn">'
                     "Move back to To action</button></div>")
    parts.append("</div>")
    return "".join(parts)


UNRANKED_RE = re.compile(r"^(.*?)\s*--\s*not ranked\b.*$")


def unranked_reason(notes: list[str]) -> str:
    """Why this finding carries no cost, in the ranker's own words.

    A card with no number next to five that have one looks like it lost its
    number. retro_rank already records the reason in the finding's notes; this
    lifts it onto the card so the absence is stated rather than inferred.
    """
    for note in notes:
        m = UNRANKED_RE.match(note.strip())
        if m:
            return m.group(1).strip()
    return ""


def render_finding(rank: int | None, f: dict, digests: dict[str, dict],
                   accepted: dict[str, dict], deferred: dict[str, dict],
                   item: dict | None = None) -> str:
    cost = f.get("cost")
    effort = f.get("effort")
    notes = f.get("notes") or []
    raw_note = next((n for n in notes if n.startswith("raw cost")), "")
    raw_m = RAW_COST_RE.search(raw_note) if raw_note else None
    sections = retro_rank.extract_sections(f.get("body", ""))
    slug = (item or {}).get("slug", "")
    key = retro_rank.normalise_title(f["title"])
    acc, dfr = accepted.get(key), deferred.get(key)
    decided = "approved" if acc else ("deferred" if dfr else "")
    updated = (acc or dfr or {}).get("date", "") or ""

    classes = "finding" + (" is-stale" if item and retro_queue.is_stale(item) else "")
    # data-* carries everything the ordering rule needs, so the browser sorts
    # from the same numbers this file sorted from rather than from DOM order.
    out = [
        f'<div class="{classes}" data-slug="{esc(slug)}"'
        f' data-cost="{"" if cost is None else f"{cost:.4f}"}"'
        f' data-title="{esc(key)}" data-decided="{decided}"'
        f' data-updated="{esc(updated)}">'
    ]
    # Left gutter: rank, cost, effort -- separated from the content by a
    # hairline. The title, not the cost, is the largest thing on the card.
    gutter = [f'<span class="rank">{"" if rank is None else f"{rank}."}</span>']
    if cost is not None:
        gutter.append(f'<span class="cost">{cost:.2f}</span>')
        if effort is not None:
            gutter.append(f'<span class="effort">effort {effort}</span>')
    else:
        gutter.append('<span class="effort">unranked</span>')
    out.append('<div class="gutter">' + "".join(gutter) + "</div>")

    out.append('<div>')
    out.append(f'<h3 class="title">{esc(f["title"])}</h3>')
    # The discount explanation is prose, so it belongs in the content column --
    # inside the 66px gutter it wraps to one word a line.
    if cost is not None and raw_m:
        out.append(f'<div class="raw">{esc(raw_note)}</div>')
    badges = []
    quick_win = cost is not None and effort is not None and retro_rank.is_quick_win(cost, effort)
    if quick_win:
        badges.append('<span class="qw">quick win</span>')
    if f["severity"] != "none":
        badges.append(f'<span class="sev bad">{esc(f["severity"])}</span>')
    if badges:
        out.append('<div class="badges">' + "".join(badges) + "</div>")
    if cost is None:
        reason = unranked_reason(notes)
        out.append('<div class="unranked-note">No cost: ' + esc(reason or "this finding could "
                   "not be ranked") + ". It sits at the bottom of the list rather than "
                   "carrying a number it has not earned.</div>")

    # Everything the human reads only when deciding *this* finding is collapsed
    # so the decision controls are reachable without a long scroll. Nothing is
    # dropped -- a citation that does not support its finding is the one failure
    # only a reader can catch.
    detail: list[str] = []
    detail.append(render_section("Problem", sections["problem"]))
    detail.append(render_section("Proposal", sections["proposal"]))
    detail.append(render_section("Measure", sections["measure"]))
    if not (sections["problem"] or sections["proposal"] or sections["measure"]) and f.get("body", "").strip():
        # Predates the problem/proposal/measure schema -- show the raw prose
        # rather than silently dropping it; it still needs re-deriving by a
        # fresh retro run, not hand-filled.
        detail.append(render_section("Notes (predates diagnosis schema)", f["body"]))
    for note in notes:
        if note.startswith("raw cost") or UNRANKED_RE.match(note.strip()):
            continue  # already shown next to the cost figure, or as the no-cost line
        detail.append(f'<div class="note">{esc(note)}</div>')
    detail.append(
        f'<div class="facts">sessions {esc("; ".join(f["sessions"]) or "none")}'
        f' &middot; recurs {"true" if f["recurs"] else "false"}'
        f' &middot; fix_files {esc("; ".join(f["fix_files"]) or "none")}'
        f' &middot; fix_lines {f["fix_lines"]}</div>'
    )
    detail.append(render_evidence(f, digests))
    out.append('<details class="detail-block"><summary>Full detail &mdash; problem, proposal, '
               "measure, evidence</summary>" + "".join(detail) + "</details>")
    # Filled by the board with state, status_detail, the approval comment and
    # the resume command. Empty and invisible until there is something to say.
    out.append('<div class="status-host"></div>')
    out.append(render_prompt_block(item, decided))
    out.append(render_decision(f["title"], item, accepted, deferred))
    # Mount point for a future per-finding comment thread. Empty by design.
    out.append('<div class="thread-slot"></div>')
    out.append("</div>")  # closes the content column
    out.append("</div>")  # closes .finding
    return "".join(out)


LIST_HEADS = {
    "toaction": (
        "To action",
        "Findings awaiting your decision. Each carries the exact prompt that will be sent, "
        "because approving dispatches a worker immediately.",
    ),
    "approved": (
        "Approved &mdash; already sent",
        "Settled. Nothing here is waiting on you, except an item that says <b>working</b> "
        "with no output for a while: that is shown in red and pulsing, and it is not "
        "progress, it is something to go and look at in the chat.",
    ),
    "deferred": (
        "Deferred",
        "Settled decisions not to act. Collapsed because it is not work.",
    ),
}


def collect(files: list[Path]) -> dict:
    """Everything a view of the findings needs, read once from disk.

    Split out of `render` so a second view cannot grow a second reader: any
    alternative rendering gets the same de-duplicated findings, the same
    accept/defer maps, the same citation digests and the same three row
    buckets, and therefore cannot silently show a different set of findings
    from the page this one generates. Pure read -- the resolved-at bookkeeping
    stays in `render`, which owns the write.
    """
    accepted_entries = load_accepted()
    deferred_entries = retro_rank.load_deferred()
    accepted = {retro_rank.normalise_title(e.get("finding", "")): e for e in accepted_entries}
    deferred = {retro_rank.normalise_title(e.get("finding", "")): e for e in deferred_entries}

    all_findings: list[dict] = []
    seen: set[str] = set()
    for path in files:
        text = path.read_text(encoding="utf-8")
        _header, findings = retro_rank.parse_findings(text)
        for f in findings:
            key = retro_rank.normalise_title(f["title"])
            if key in seen:
                continue  # first occurrence wins, matching retro_queue.build()
            seen.add(key)
            all_findings.append(f)

    cited = [f for f in all_findings if f["human_turns"] or f["mechanical"]]
    digests: dict[str, dict] = {}
    if cited:
        digests, warnings, _personas = retro_rank.citation_report(cited)
        for w in warnings:
            print(f"warning: {w}", file=sys.stderr)

    rows: dict[str, list[dict]] = {"toaction": [], "approved": [], "deferred": []}
    for f in all_findings:
        key = retro_rank.normalise_title(f["title"])
        acc, dfr = accepted.get(key), deferred.get(key)
        kind = "approved" if acc else ("deferred" if dfr else "toaction")
        rows[kind].append({
            "finding": f,
            "title": key,
            "cost": f.get("cost"),
            # No board here, so no live state: every statically-settled row
            # sorts at UNKNOWN_STATE_RANK and is separated by date. The board
            # re-sorts with real states the moment it answers.
            "state": "",
            "updated_at": (acc or dfr or {}).get("date", "") or "",
        })

    return {
        "findings": all_findings,
        "digests": digests,
        "accepted": accepted,
        "deferred": deferred,
        "accepted_entries": accepted_entries,
        "rows": rows,
    }


def render(files: list[Path]) -> str:
    """Render every docs/retro/*-findings.md file to one HTML document.

    Three lists, not one report (decision 3, with deferred split out of
    approved). They are separate blocks with their own headings and borders,
    not headings in a shared flow: a settled item read as one awaiting a
    decision is the failure this page exists to prevent. Within each list the
    order comes from `order_rows`, and the board re-partitions the same cards
    live using the same rule, which is why the card, not the list, carries the
    data the sort reads.
    """
    data = collect(files)
    all_findings = data["findings"]
    digests = data["digests"]
    accepted = data["accepted"]
    deferred = data["deferred"]
    accepted_entries = data["accepted_entries"]
    rows = data["rows"]

    present = {retro_rank.normalise_title(f["title"]) for f in all_findings}
    if mark_resolved_if_absent(accepted_entries, present):
        save_accepted(accepted_entries)

    def cards_for(kind: str) -> list[str]:
        # Every card in a list is numbered by its position in that list, or
        # none is -- a page where 2 of 5 carry a number reads as broken.
        return [
            render_finding(i, row["finding"], digests, accepted, deferred,
                           retro_queue.load_by_title(row["finding"]["title"]))
            for i, row in enumerate(order_rows(kind, rows[kind]), start=1)
        ]

    empties = {
        "toaction": "Nothing awaiting review.",
        "approved": "Nothing approved yet.",
        "deferred": "Nothing deferred.",
    }

    def inner(kind: str) -> list[str]:
        _title, note = LIST_HEADS[kind]
        cards = cards_for(kind)
        return [
            f'<p class="list-note">{note}</p>',
            f'<p class="list-caption">{ORDER_CAPTIONS[kind]}</p>',
            f'<div id="list-{kind}">',
            f'<p class="list-empty"{"" if not cards else " style=display:none"}>'
            f"{empties[kind]}</p>",
            *cards,
            "</div>",
        ]

    def panel(kind: str, hidden: bool) -> list[str]:
        return [
            f'<div class="panel" id="panel-{kind}"{" hidden" if hidden else ""}>',
            *inner(kind),
            "</div>",
        ]

    TAB_LABELS = {"toaction": "To action", "approved": "Activity", "deferred": "Deferred"}
    body: list[str] = [
        '<div id="wedge-banner" class="banner" hidden></div>',
        '<div class="tabs" role="tablist">',
    ]
    for i, kind in enumerate(("toaction", "approved", "deferred")):
        body.append(
            f'<button type="button" class="tab" role="tab" '
            f'aria-selected="{"true" if i == 0 else "false"}" data-p="panel-{kind}">'
            f'{TAB_LABELS[kind]} <span class="n" data-count-for="{kind}">{len(rows[kind])}</span>'
            "</button>"
        )
    body.append("</div>")
    body += panel("toaction", hidden=False)
    body += panel("approved", hidden=True)
    body += panel("deferred", hidden=True)

    board_url = board_client.last_known_board_url()
    doc = [
        "<!doctype html><html lang=en><head><meta charset=utf-8>",
        '<meta name=viewport content="width=device-width,initial-scale=1">',
        "<title>Retrospective findings</title>"
        f"<style>{plan_html.CSS}{CSS_EXTRA}{board_client.NAV_CSS}"
        f"{board_client.CSS}</style></head><body>",
        '<div class="wrap">',
        board_client.navigation_html(),
        '<h1>Retrospective findings</h1>',
        '<p class="sub">Generated by tools/retro_html.py from docs/retro/*-findings.md. '
        "Ranked by cost -- what this cost the human -- with effort -- how cheap it is to fix -- "
        "shown beside it, never folded in.</p>",
        board_client.shell_html(),
        '<div id="board-status" class="board-only"></div>',
    ]
    doc.extend(body)
    doc.append(board_client.core_js(board_url + "retro.html" if board_url else ""))
    doc.append(DISPATCH_JS)
    doc.append("</div></body></html>")
    return "\n".join(doc)


def latest_and_all_findings_files() -> list[Path]:
    return sorted(RETRO_DIR.glob("*-findings.md"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stdout", action="store_true", help="print instead of writing")
    args = ap.parse_args()

    files = latest_and_all_findings_files()
    if not files:
        print("no docs/retro/*-findings.md file found", file=sys.stderr)
        return 1

    doc = render(files)
    unreviewed, highest_cost = retro_rank.unreviewed_summary()
    if args.stdout:
        print(doc)
        return 0
    OUT.write_text(doc, encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}")
    if unreviewed:
        print(f"{unreviewed} finding(s) awaiting review -- highest cost {highest_cost:.2f}")
    _print_board_url()
    return 0


def _print_board_url() -> None:
    """Start (or confirm) the local board and print its URL.

    Imported lazily -- see plan_html.py's own `_print_board_url` for why:
    tools/board.py imports this module at its own top level, so importing it
    back here at module load would be a real cycle.
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import board
        url = board.ensure_running()
    except Exception as exc:  # noqa: BLE001 -- the board is optional, never fatal
        print(f"board: not started ({exc})")
        return
    if url:
        print(f"board: {url}retro.html")
    else:
        print("board: not started")


if __name__ == "__main__":
    raise SystemExit(main())
