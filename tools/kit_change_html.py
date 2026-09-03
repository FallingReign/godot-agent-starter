#!/usr/bin/env python3
"""Render one complete install or upgrade review for the local cockpit.

The renderer is deliberately pure with respect to the target project.  It
accepts an already prepared preview/status mapping, escapes every displayed
value, and returns HTML.  It never discovers, opens, or imports a target file.

The page remains a complete readable review when JavaScript or the cockpit is
unavailable.  Interactive actions use the shared Board client, so capability,
same-origin, stale-version, retry, and request-error behaviour stay aligned
with plan.html and retro.html.
"""
from __future__ import annotations

import html
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

import board_client


STATUS_LABELS = {
    "scanning": "Scanning",
    "ready": "Ready",
    "needs_decision": "{count} decision needed",
    "blocked": "Blocked",
    "applying": "Applying",
    "checking": "Running Apply-time check",
    "complete": "Applied; Apply-time check passed",
    "adoption_required": "Applied; problems recorded at Apply",
    "recovery_required": "Recovery needed",
    "restored": "Previous state restored",
    "failed": "Could not finish",
}

STATUS_STEP = {
    "scanning": "scan",
    "ready": "review",
    "needs_decision": "review",
    "blocked": "review",
    "applying": "apply",
    "checking": "check",
    "complete": "check",
    "adoption_required": "check",
    "recovery_required": "check",
    "restored": "check",
    "failed": "check",
}

STEP_LABELS = (
    ("scan", "Scan"),
    ("review", "Review"),
    ("apply", "Apply"),
    ("check", "Apply-time check"),
)

COUNT_FIELDS = (
    ("kit_files", "Kit files added or updated"),
    ("shared_files", "Shared files receiving a kit section"),
    ("removed_files", "Old kit files removed"),
    ("game_files", "Game files changed"),
)

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_CHECKED_AT_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
)
_SAFE_KIT_REVIEW_URL = re.compile(
    r"http://127\.0\.0\.1:(?P<port>[0-9]{1,5})/kit-change\.html"
    r"(?:\?session=(?P<session>[0-9a-f]{64}))?"
)
_SAFE_PLAN_URL = re.compile(
    r"(?:plan\.html|http://127\.0\.0\.1:(?P<port>[0-9]{1,5})/plan\.html)"
)


def _text(value: object, default: str = "") -> str:
    if value is None:
        return default
    rendered = str(value).strip()
    return rendered if rendered else default


def _esc(value: object, default: str = "") -> str:
    return html.escape(_text(value, default), quote=True)


def _object(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _items(value: object) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _integer(value: object) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, number)


def _check_evidence(value: object) -> tuple[str, str]:
    evidence = _object(value)
    state = _text(evidence.get("state"), "not_run")
    checked_at = _text(evidence.get("checked_at"))
    if state != "apply_time":
        return "not_run", ""
    if checked_at and _CHECKED_AT_RE.fullmatch(checked_at) is None:
        checked_at = ""
    return "apply_time", checked_at


def _inline_json(value: object) -> str:
    return (
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def _safe_review_link(value: object) -> str:
    raw = _text(value)
    match = _SAFE_KIT_REVIEW_URL.fullmatch(raw)
    if match is None:
        return ""
    port_text = str(match.group("port"))
    if not (1 <= int(port_text) <= 65535) or port_text != str(int(port_text)):
        return ""
    escaped = html.escape(raw, quote=True)
    return f'<a href="{escaped}">{escaped}</a>'


def _safe_plan_url(value: object) -> str:
    raw = _text(value)
    match = _SAFE_PLAN_URL.fullmatch(raw)
    if match is None:
        return ""
    port_text = match.group("port")
    if port_text is not None:
        if not (1 <= int(port_text) <= 65535) or port_text != str(int(port_text)):
            return ""
    return raw


def _decision_count(decisions: Sequence[Mapping[str, Any]]) -> int:
    missing = 0
    for item in decisions:
        selected = _text(item.get("selected"))
        values = {_text(choice.get("value")) for choice in _items(item.get("choices"))}
        if not selected or selected not in values:
            missing += 1
    return missing


def _status_label(status: str, decision_count: int) -> str:
    if status == "needs_decision":
        count = decision_count or 1
        noun = "decision" if count == 1 else "decisions"
        return f"{count} {noun} needed"
    return STATUS_LABELS.get(status, STATUS_LABELS["blocked"])


def _effective_status(
    raw_status: object,
    digest: str,
    game_files: int,
) -> tuple[str, list[str]]:
    status = _text(raw_status, "blocked").lower().replace("-", "_")
    blockers: list[str] = []
    if status not in STATUS_LABELS:
        status = "blocked"
        blockers.append("The kit change status is not recognised.")
    if not _DIGEST_RE.fullmatch(digest):
        status = "blocked"
        blockers.append("The exact change fingerprint is missing.")
    if game_files:
        status = "blocked"
        blockers.append(
            "This kit change includes game files. Review the plan before continuing."
        )
    return status, blockers


def _step_html(status: str) -> str:
    current = STATUS_STEP[status]
    order = [name for name, _label in STEP_LABELS]
    current_index = order.index(current)
    rows: list[str] = []
    for index, (name, label) in enumerate(STEP_LABELS):
        state = "complete" if index < current_index else "upcoming"
        if name == current:
            state = "current"
        aria = ' aria-current="step"' if state == "current" else ""
        rows.append(
            f'<li class="step {state}" data-step="{name}"{aria}>'
            f'<span class="step-number" aria-hidden="true">{index + 1}</span>'
            f"<span>{label}</span></li>"
        )
    return (
        '<ol class="steps" aria-label="Kit change progress">'
        + "".join(rows)
        + "</ol>"
    )


def _decision_html(decisions: Sequence[Mapping[str, Any]]) -> str:
    if not decisions:
        return ""
    cards: list[str] = []
    for index, decision in enumerate(decisions, start=1):
        decision_id = _text(decision.get("id"), f"D{index}")
        question = _text(decision.get("question"), "A choice is needed.")
        selected = _text(decision.get("selected"))
        choices = _items(decision.get("choices"))
        options: list[str] = []
        for choice_index, choice in enumerate(choices, start=1):
            value = _text(choice.get("value"), f"choice-{choice_index}")
            label = _text(choice.get("label"), value)
            description = _text(choice.get("description"))
            recommended = bool(choice.get("recommended"))
            checked = " checked" if value == selected else ""
            recommendation = (
                '<span class="recommended">Recommended</span>' if recommended else ""
            )
            description_html = (
                f'<span class="choice-description">{_esc(description)}</span>'
                if description
                else ""
            )
            options.append(
                '<label class="choice">'
                f'<input type="radio" name="decision-{_esc(decision_id)}" '
                f'value="{_esc(value)}" data-decision-id="{_esc(decision_id)}" '
                f"data-board-control disabled{checked}>"
                '<span class="choice-copy">'
                f'<span class="choice-label">{_esc(label)}{recommendation}</span>'
                f"{description_html}</span></label>"
            )
        if not options:
            options.append(
                '<p class="blocker">No safe choices were provided. The change cannot continue.</p>'
            )
        cards.append(
            f'<fieldset class="decision" data-decision="{_esc(decision_id)}">'
            f'<legend><span class="decision-code">{_esc(decision_id)}</span> '
            f"{_esc(question)}</legend>"
            + "".join(options)
            + "</fieldset>"
        )
    return (
        '<section class="section decisions" id="kit-change-decisions" '
        'aria-labelledby="decisions-title"><h2 id="decisions-title">'
        "Needs your decision</h2>"
        + "".join(cards)
        + "</section>"
    )


def _files_html(files: Sequence[Mapping[str, Any]]) -> str:
    if not files:
        rows = '<li class="empty">No file details were supplied.</li>'
    else:
        rendered: list[str] = []
        for item in files:
            path = _text(item.get("path"), "Unnamed file")
            action = _text(item.get("action"), "Review")
            reason = _text(item.get("reason"))
            reason_html = f'<span class="file-reason">{_esc(reason)}</span>' if reason else ""
            rendered.append(
                '<li class="file-row">'
                f'<span class="file-action">{_esc(action)}</span>'
                f'<code>{_esc(path)}</code>{reason_html}</li>'
            )
        rows = "".join(rendered)
    return (
        '<details class="file-details" id="kit-change-files">'
        '<summary>Show file details</summary>'
        f'<ul class="file-list">{rows}</ul></details>'
    )


def _problems_html(
    issues: Sequence[Mapping[str, Any]], *, checked: bool
) -> str:
    rows: list[str] = []
    for issue in issues:
        stage = _text(issue.get("stage"), "check")
        code = _text(issue.get("code"), "problem")
        path = _text(issue.get("path"), "Unknown file")
        line = _integer(issue.get("line"))
        location = f"{path}:{line}" if line else path
        rows.append(
            '<li class="problem-row">'
            f'<code class="problem-location">{_esc(location)}</code>'
            f'<span class="problem-check">{_esc(stage)} — {_esc(code)}</span>'
            "</li>"
        )
    hidden = "" if checked and rows else " hidden"
    summary = f"Show problems to address ({len(rows)})"
    return (
        f'<details class="problem-details" id="kit-change-problems"{hidden}>'
        f'<summary id="kit-change-problems-summary">{_esc(summary)}</summary>'
        f'<ul class="problem-list" id="kit-change-problem-list">{"".join(rows)}</ul>'
        "</details>"
    )


def _styles() -> str:
    return """
:root{color-scheme:dark;--bg:#111318;--card:#181c24;--line:#303744;
--text:#f2f4f7;--dim:#aeb6c4;--accent:#ff7a35;--ok:#78d99b;--bad:#ff8790;
--warn:#f2cf72;--acc:var(--accent);font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);line-height:1.45}
main{width:min(880px,calc(100% - 28px));margin:0 auto;padding:28px 0 64px}
h1,h2,p{margin-top:0}h1{font-size:clamp(1.8rem,5vw,2.65rem);line-height:1.08;margin-bottom:8px}
h2{font-size:1.15rem;margin-bottom:14px}.eyebrow{color:var(--accent);font-size:.78rem;
font-weight:800;letter-spacing:.09em;text-transform:uppercase;margin-bottom:7px}
.subtitle{color:var(--dim);max-width:65ch}.steps{list-style:none;margin:24px 0;padding:0;
display:grid;grid-template-columns:repeat(4,1fr);gap:8px}.step{display:flex;align-items:center;
gap:8px;color:var(--dim);padding:10px;border-bottom:3px solid var(--line)}
.step-number{display:grid;place-items:center;width:24px;height:24px;border:1px solid currentColor;border-radius:50%}
.step.current{color:var(--text);border-color:var(--accent);font-weight:750}.step.complete{color:var(--ok)}
.status-card,.section,.connection-guidance{background:var(--card);border:1px solid var(--line);
border-radius:10px;padding:18px;margin:14px 0}.status-card{border-left:5px solid var(--accent)}
.status-label{font-size:1.35rem;font-weight:800;margin:0}.status-note{color:var(--dim);margin:5px 0 0}
.status-next{font-weight:750;margin:10px 0 0}
.facts,.check-list{display:grid;grid-template-columns:minmax(130px,1fr) minmax(180px,2fr);
margin:0;gap:0}.facts div,.check-list div{display:contents}.facts dt,.facts dd,.check-list dt,
.check-list dd{padding:9px 0;border-bottom:1px solid var(--line);margin:0}.facts dt,.check-list dt{color:var(--dim)}
.facts dd,.check-list dd{font-weight:700}.safe{color:var(--ok)}.warning{color:var(--warn)}
.blockers{border-color:#733039;background:#28171b}.blocker{color:#ffd6d9;margin:7px 0}
.decision{border:1px solid var(--line);border-radius:8px;padding:14px;margin:12px 0}
.decision legend{font-weight:800;padding:0 6px}.decision-code{color:var(--accent)}
.choice{display:flex;gap:10px;align-items:flex-start;padding:10px;border-radius:7px;cursor:pointer}
.choice:hover{background:#202630}.choice input{margin-top:4px}.choice-copy{display:flex;flex-direction:column;gap:3px}
.choice-label{font-weight:750}.choice-description,.file-reason{color:var(--dim);font-size:.9rem}
.recommended{display:inline-block;margin-left:8px;color:var(--ok);font-size:.72rem;
font-weight:800;text-transform:uppercase}.counts{list-style:none;padding:0;margin:0}.counts li{display:flex;
justify-content:space-between;gap:18px;padding:8px 0;border-bottom:1px solid var(--line)}
.count{font-weight:800}.file-details,.problem-details{margin-top:14px}
.file-details summary,.problem-details summary{cursor:pointer;font-weight:750}
.file-list{list-style:none;padding:8px 0 0;margin:0}.file-row{display:grid;grid-template-columns:90px minmax(0,1fr);
gap:6px 12px;padding:9px 0;border-bottom:1px solid var(--line)}.file-action{color:var(--accent);font-weight:750}
.file-row code{overflow-wrap:anywhere}.file-reason{grid-column:2}.fingerprint code{display:block;
overflow-wrap:anywhere;margin-top:7px;color:var(--dim)}.gap-note{border-left:4px solid var(--warn);padding-left:12px}
.problem-list{list-style:none;padding:8px 0 0;margin:0}.problem-row{display:flex;flex-direction:column;
gap:3px;padding:9px 0;border-bottom:1px solid var(--line)}.problem-location{overflow-wrap:anywhere}
.problem-check{color:var(--dim);font-size:.9rem}.provider-handoff{border-left:5px solid var(--ok)}
.actions{display:flex;gap:10px;flex-wrap:wrap;align-items:center}.primary,.secondary,.retry{
appearance:none;border:1px solid var(--accent);background:var(--accent);color:#111318;border-radius:7px;
font:inherit;font-weight:800;padding:10px 16px;cursor:pointer}.secondary,.retry{background:transparent;color:var(--text);
border-color:var(--line)}button:focus-visible,input:focus-visible,summary:focus-visible,a:focus-visible{
outline:3px solid #fff;outline-offset:3px}button[disabled],input[disabled]{opacity:.48;cursor:not-allowed}
.connection-guidance{display:none;border-color:#6f5e25;background:#262211}
.board-file #kit-change-file-guidance,.board-down #kit-change-down-guidance{display:block}
.kit-change-page.board-file #board-banner,.kit-change-page.board-down #board-banner{display:none!important}
.connection-guidance a{color:var(--accent);overflow-wrap:anywhere}.technical summary{cursor:pointer;color:var(--dim)}
.technical code{overflow-wrap:anywhere}.empty{color:var(--dim)}
@media(max-width:600px){main{width:min(100% - 20px,880px);padding-top:18px}.steps{grid-template-columns:1fr 1fr}
.facts,.check-list{grid-template-columns:1fr}.facts div,.check-list div{display:block;border-bottom:1px solid var(--line)}
.facts dt,.facts dd,.check-list dt,.check-list dd{border:0;padding:5px 0}.file-row{grid-template-columns:1fr}
.file-reason{grid-column:1}}
"""


def _client_script(static: Mapping[str, Any]) -> str:
    return r"""
<script>
(function(){
  "use strict";
  var STATIC = __STATIC__;
  var status = String(STATIC.status || "blocked");
  var active = {
    applying:"Applying the reviewed kit change.",
    checking:"Running the Apply-time check."
  };
  var labels = __LABELS__;
  var steps = {scanning:"scan",ready:"review",needs_decision:"review",blocked:"review",
               applying:"apply",checking:"check",complete:"check",adoption_required:"check",
               recovery_required:"check",restored:"check",failed:"check"};
  var controls = document.getElementById("kit-change-controls");
  var applyButton = document.getElementById("kit-change-apply");
  var recoverButton = document.getElementById("kit-change-recover");
  var restoreButton = document.getElementById("kit-change-restore");
  var reviewButton = document.getElementById("kit-change-review-again");
  var openButton = document.getElementById("kit-change-open-plan");
  var statusLabel = document.getElementById("kit-change-status-label");
  var statusNote = document.getElementById("kit-change-status-note");
  var nextStep = document.getElementById("kit-change-next-step");
  var checkedAt = document.getElementById("kit-change-checked-at");
  var checkScope = document.getElementById("kit-change-check-scope");
  var kitFilesCheck = document.getElementById("kit-change-kit-files-check");
  var newWorkCheck = document.getElementById("kit-change-new-work-check");
  var gapCount = document.getElementById("kit-change-gap-count");
  var gapNote = document.getElementById("kit-change-gap-note");
  var problemDetails = document.getElementById("kit-change-problems");
  var problemSummary = document.getElementById("kit-change-problems-summary");
  var problemList = document.getElementById("kit-change-problem-list");
  var providerHandoff = document.getElementById("kit-change-provider-handoff");
  var resultSha = String(STATIC.result_sha256 || "");
  var recoveryAction = String(STATIC.recovery_action || "");
  var planUrl = String(STATIC.plan_url || "");
  var sessionId = String(STATIC.session_id || "");
  var checkEvidence = STATIC.check_evidence || {state:"not_run",checked_at:""};

  function decisionValues(){
    var values = {}, groups = document.querySelectorAll("[data-decision]");
    for(var i=0;i<groups.length;i++){
      var id = String(groups[i].getAttribute("data-decision") || "");
      var chosen = groups[i].querySelector("input[type=radio]:checked");
      if(chosen) values[id] = String(chosen.value || "");
    }
    return values;
  }
  function allDecisionsAnswered(){
    var groups = document.querySelectorAll("[data-decision]");
    for(var i=0;i<groups.length;i++){
      if(!groups[i].querySelector("input[type=radio]:checked")) return false;
    }
    return true;
  }
  function unresolvedDecisionCount(){
    var groups = document.querySelectorAll("[data-decision]"), count = 0;
    for(var i=0;i<groups.length;i++){
      if(!groups[i].querySelector("input[type=radio]:checked")) count++;
    }
    return count;
  }
  function safeDigest(value){ return /^[0-9a-f]{64}$/.test(String(value || "")); }
  function safeCheckedAt(value){
    value = String(value || "");
    return /^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$/.test(value)
      ? value : "";
  }
  function safePlanUrl(value){
    value = String(value || "");
    if(value === "plan.html") return value;
    var match = /^http:\/\/127\.0\.0\.1:([0-9]{1,5})\/plan\.html$/.exec(value);
    if(!match) return "";
    var port = Number(match[1]);
    return port >= 1 && port <= 65535 && String(port) === match[1] ? value : "";
  }
  function safeReviewUrl(value){
    value = String(value || "");
    var match = /^http:\/\/127\.0\.0\.1:([0-9]{1,5})\/kit-change\.html\?session=([0-9a-f]{64})$/.exec(value);
    if(!match) return "";
    var port = Number(match[1]);
    return port >= 1 && port <= 65535 && String(port) === match[1] ? value : "";
  }
  function renderProblems(gaps,checked){
    while(problemList.firstChild) problemList.removeChild(problemList.firstChild);
    var issues = gaps && Array.isArray(gaps.issues) ? gaps.issues.slice(0,4096) : [];
    for(var i=0;i<issues.length;i++){
      var issue = issues[i] && typeof issues[i] === "object" ? issues[i] : {};
      var path = String(issue.path || "Unknown file");
      var line = Math.max(0,Number(issue.line) || 0);
      var location = document.createElement("code");
      location.className = "problem-location";
      location.textContent = path + (line ? ":" + String(line) : "");
      var check = document.createElement("span");
      check.className = "problem-check";
      check.textContent = String(issue.stage || "check") + " — "
        + String(issue.code || "problem");
      var row = document.createElement("li");
      row.className = "problem-row";
      row.appendChild(location);
      row.appendChild(check);
      problemList.appendChild(row);
    }
    problemSummary.textContent = "Show problems to address (" + String(issues.length) + ")";
    problemDetails.hidden = !(checked && issues.length);
  }
  function labelFor(value, count){
    if(value === "needs_decision"){
      return String(count || 1) + ((count || 1) === 1 ? " decision needed" : " decisions needed");
    }
    return labels[value] || labels.blocked;
  }
  function applyStep(value){
    var current = steps[value] || "review";
    var nodes = document.querySelectorAll("[data-step]");
    var seen = false;
    for(var i=0;i<nodes.length;i++){
      var name = nodes[i].getAttribute("data-step");
      nodes[i].className = "step " + (name === current ? "current" : (seen ? "upcoming" : "complete"));
      if(name === current){ nodes[i].setAttribute("aria-current","step"); seen = true; }
      else nodes[i].removeAttribute("aria-current");
    }
  }
  function updateActions(){
    var live = window.Board && Board.mode() === "live";
    var reviewable = status === "ready" || status === "needs_decision";
    var complete = status === "complete" || status === "adoption_required";
    var recovering = status === "recovery_required";
    var ended = status === "restored" || status === "failed";
    var unresolved = unresolvedDecisionCount();
    if(status === "needs_decision"){
      statusLabel.textContent = unresolved ? labelFor(status,unresolved) : labels.ready;
      statusNote.textContent = unresolved
        ? "Answer the choices below before continuing."
        : "The exact kit change is ready for review.";
    }
    controls.disabled = !live || !(reviewable || complete || recovering || ended);
    var radios = document.querySelectorAll("[data-decision-id]");
    for(var i=0;i<radios.length;i++) radios[i].disabled = !live || !reviewable;
    applyButton.hidden = !reviewable;
    applyButton.disabled = !live || unresolved !== 0 || !allDecisionsAnswered();
    recoverButton.hidden = !(recovering && recoveryAction === "recover");
    recoverButton.disabled = !live || !sessionId;
    restoreButton.hidden = !(complete || (recovering && recoveryAction === "restore"));
    restoreButton.disabled = !live || !resultSha;
    restoreButton.textContent = recovering ? "Retry restore" : "Restore previous state";
    nextStep.hidden = !recovering;
    nextStep.textContent = recoveryAction === "restore"
      ? "Next: Retry restore. This only finishes restoring the previous state."
      : "Next: Retry recovery. This only finishes restoring the previous state.";
    openButton.hidden = !complete || !planUrl;
    openButton.disabled = !live || !planUrl;
    reviewButton.hidden = !ended;
    reviewButton.disabled = !live;
  }
  function setStatus(next, detail, state){
    if(!steps[next]) next = "blocked";
    status = next;
    document.body.setAttribute("data-kit-change-status",status);
    var unresolved = unresolvedDecisionCount();
    statusLabel.textContent = labelFor(status, unresolved);
    statusNote.textContent = detail || active[status] || "";
    if(state){
      var nextResult = String(state.result_sha256 || resultSha || "");
      var nextPlanUrl = String(state.plan_url || planUrl || "");
      resultSha = safeDigest(nextResult) ? nextResult : "";
      recoveryAction = next === "recovery_required" ? (resultSha ? "restore" : "recover") : "";
      planUrl = safePlanUrl(nextPlanUrl);
      if(state.check_evidence && typeof state.check_evidence === "object"){
        checkEvidence = {
          state:String(state.check_evidence.state || "not_run"),
          checked_at:safeCheckedAt(state.check_evidence.checked_at)
        };
      }
      var gaps = state.existing_gaps;
      if(gaps && typeof gaps === "object"){
        var count = Math.max(0,Number(gaps.count) || 0);
        var checkedGaps = String(gaps.status || "") === "checked"
          && String(checkEvidence.state || "") === "apply_time";
        gapCount.textContent = checkedGaps ? (count ? String(count) : "None") : "Not checked";
        gapNote.hidden = !(checkedGaps && count);
        gapNote.textContent = checkedGaps && count
          ? "The Apply-time check recorded " + String(count) + " existing "
            + (count === 1 ? "problem" : "problems")
            + ". It did not hide them or mark them as fixed. "
            + "Later project changes are not included."
          : "";
        renderProblems(gaps,checkedGaps);
      }
    }
    var checked = status === "complete" || status === "adoption_required";
    var applyTime = checked && String(checkEvidence.state || "") === "apply_time";
    var time = safeCheckedAt(checkEvidence.checked_at);
    checkedAt.textContent = applyTime
      ? (time || "Time not recorded (older session)") : "Not run";
    checkScope.textContent = applyTime
      ? "This check ran when Apply finished. Later project changes are not included."
      : "The Apply-time check has not finished.";
    kitFilesCheck.textContent = applyTime ? "Passed at Apply" : "Not checked";
    newWorkCheck.textContent = status === "complete" && applyTime ? "Passed at Apply" :
      (status === "adoption_required" && applyTime
        ? "Problems recorded at Apply" : "Not checked");
    providerHandoff.hidden = !(status === "complete" || status === "adoption_required");
    applyStep(status);
    updateActions();
  }
  function stateFrom(state){
    var next = state && state.kit_change ? state.kit_change : null;
    return next && String(next.session_id || "") === sessionId ? next : null;
  }
  function acceptResponse(result){
    if(!result || !result.ok){ Board.reportIfFailed(result); return; }
    var nextReview = safeReviewUrl(result.data && result.data.review_url);
    if(nextReview){ location.href = nextReview; return; }
    var next = stateFrom(result.data) || result.data || {};
    setStatus(String(next.status || status),String(next.detail || ""),next);
    Board.refresh();
  }
  function applyChange(){
    return Board.guard(applyButton,"Applying\u2026",function(){
      return Board.request("/api/kit-change/apply",{method:"POST",body:{
        session_id:sessionId,plan_sha256:STATIC.plan_sha256,choices:decisionValues()
      }}).then(acceptResponse);
    });
  }
  function restoreChange(){
    return Board.guard(restoreButton,"Restoring\u2026",function(){
      return Board.request("/api/kit-change/restore",{method:"POST",body:{
        session_id:sessionId,result_sha256:resultSha
      }}).then(acceptResponse);
    });
  }
  function recoverChange(){
    return Board.guard(recoverButton,"Recovering\u2026",function(){
      return Board.request("/api/kit-change/recover",{method:"POST",body:{
        session_id:sessionId
      }}).then(acceptResponse);
    });
  }
  applyButton.addEventListener("click",applyChange);
  recoverButton.addEventListener("click",recoverChange);
  restoreButton.addEventListener("click",restoreChange);
  reviewButton.addEventListener("click",function(){ location.reload(); });
  openButton.addEventListener("click",function(){ if(planUrl) location.href = planUrl; });
  var radios = document.querySelectorAll("[data-decision-id]");
  for(var i=0;i<radios.length;i++) radios[i].addEventListener("change",updateActions);
  var retry = document.getElementById("kit-change-retry");
  if(retry) retry.addEventListener("click",function(){ Board.refresh().then(updateActions); });
  Board.onState(function(boardState){
    var next = stateFrom(boardState);
    if(next) setStatus(String(next.status || status),String(next.detail || ""),next);
    else updateActions();
  });
  setStatus(status,STATIC.detail || "",STATIC);
})();
</script>
""".replace("__STATIC__", _inline_json(static)).replace(
        "__LABELS__", _inline_json(STATUS_LABELS)
    )


def render(preview: Mapping[str, Any], board_url_hint: str = "") -> str:
    """Return a complete install/upgrade review without reading target files."""
    data = _object(preview)
    mode = _text(data.get("mode"), "install").lower()
    if mode not in ("install", "upgrade"):
        mode = "install"
    project = _object(data.get("project"))
    project_name = _text(project.get("name"), "This project")
    project_path = _text(project.get("path"), "Not supplied")
    current_version = _text(data.get("current_version"), "Not installed")
    incoming_version = _text(data.get("incoming_version"), "Not supplied")
    digest = _text(data.get("plan_sha256")).lower()
    raw_result_sha = _text(data.get("result_sha256")).lower()
    result_sha = raw_result_sha if _DIGEST_RE.fullmatch(raw_result_sha) else ""
    evidence_state, checked_at = _check_evidence(data.get("check_evidence"))
    counts = _object(data.get("counts"))
    count_values = {name: _integer(counts.get(name)) for name, _label in COUNT_FIELDS}
    decisions = _items(data.get("decisions"))
    missing_decisions = _decision_count(decisions)
    status, safety_blockers = _effective_status(
        data.get("status"), digest, count_values["game_files"]
    )
    supplied_blockers = [
        _text(item)
        for item in data.get("blockers", [])
        if _text(item)
    ] if isinstance(data.get("blockers"), list) else []
    decision_blockers = [
        item for item in supplied_blockers if item.startswith("[game-root-ambiguous]")
    ]
    other_blockers = [item for item in supplied_blockers if item not in decision_blockers]
    if missing_decisions and decision_blockers and not safety_blockers and not other_blockers:
        status = "needs_decision"
        blockers: list[str] = []
    else:
        blockers = safety_blockers + supplied_blockers
    if blockers:
        status = "blocked"
    if status == "recovery_required" and raw_result_sha and not result_sha:
        status = "blocked"
        blockers.append("The exact recovery fingerprint is malformed.")
    if (
        "check_evidence" not in data
        and status in {"complete", "adoption_required"}
        and result_sha
    ):
        evidence_state = "apply_time"

    title = "Add kit to this project" if mode == "install" else "Upgrade this kit"
    action = (
        "Review this folder"
        if status == "needs_decision"
        else "Add kit"
        if mode == "install"
        else "Upgrade kit"
    )
    status_label = _status_label(status, missing_decisions)
    detail = _text(data.get("detail"))
    if not detail:
        detail = {
            "scanning": "Reading the project without changing it.",
            "ready": "The exact kit change is ready for review.",
            "needs_decision": "Answer the choices below before continuing.",
            "blocked": "Resolve the blockers below before continuing.",
            "applying": "Applying the reviewed kit change.",
            "checking": "Running the Apply-time check without starting Godot.",
            "complete": "The Apply-time check passed. Later project changes are not included.",
            "adoption_required": "Project problems were recorded at Apply. Later project changes are not included.",
            "recovery_required": "The previous project state still needs to be restored.",
            "restored": "The project is back to its previous state.",
            "failed": "The kit change did not finish.",
        }[status]

    design_value = data.get("design")
    if isinstance(design_value, Mapping):
        design = _text(design_value.get("label") or design_value.get("status"), "Not checked")
    else:
        design = _text(design_value, "Not checked")
    gaps_value = data.get("existing_gaps")
    if isinstance(gaps_value, Mapping):
        gap_count = _integer(gaps_value.get("count"))
        gap_issues = _items(gaps_value.get("issues"))
        gaps_checked = (
            _text(gaps_value.get("status"), "not_checked") == "checked"
            and evidence_state == "apply_time"
        )
    else:
        gap_count = _integer(gaps_value)
        gap_issues = []
        gaps_checked = evidence_state == "apply_time"
    recovery = _text(data.get("recovery"), "Previous state will be saved")
    review_url = _text(board_url_hint or data.get("review_url"))
    review_link = _safe_review_link(review_url)
    plan_url = _safe_plan_url(data.get("plan_url"))
    files = _items(data.get("files"))
    recovery_action = (
        "restore" if status == "recovery_required" and result_sha
        else "recover" if status == "recovery_required"
        else ""
    )

    blocker_html = ""
    if blockers:
        blocker_html = (
            '<section class="section blockers" aria-labelledby="blockers-title">'
            '<h2 id="blockers-title">Cannot continue</h2>'
            + "".join(f'<p class="blocker">{_esc(item)}</p>' for item in blockers)
            + "</section>"
        )

    counts_html = "".join(
        f"<li><span>{label}</span><span class=\"count\">{count_values[name]}</span></li>"
        for name, label in COUNT_FIELDS
    )
    game_label = "No changes" if count_values["game_files"] == 0 else str(count_values["game_files"])
    game_class = "safe" if count_values["game_files"] == 0 else "warning"
    gaps_label = ("None" if gap_count == 0 else str(gap_count)) if gaps_checked else "Not checked"
    gap_note = '<p class="gap-note" id="kit-change-gap-note" hidden></p>'
    if gap_count and gaps_checked:
        noun = "problem" if gap_count == 1 else "problems"
        gap_note = (
            '<p class="gap-note" id="kit-change-gap-note">'
            f"The Apply-time check recorded {gap_count} existing {noun}. "
            "It did not hide them or mark them as fixed. "
            "Later project changes are not included.</p>"
        )
    problems_html = _problems_html(gap_issues, checked=gaps_checked)

    link_copy = review_link or "the loopback link printed by your agent"
    if recovery_action:
        retry_name = "restore" if recovery_action == "restore" else "recovery"
        no_script_decision = (
            f"Recovery is still needed. Tell your agent to retry {retry_name}. "
            "The retry only finishes restoring the previous state."
        )
    elif missing_decisions:
        no_script_decision = (
            "A decision is still needed. Tell your agent your choice first. "
            "It will refresh this review before applying anything."
        )
    else:
        no_script_decision = (
            "Tell Codex or Copilot that you approve this exact kit change. "
            "The agent can apply it without asking you to run a command."
        )

    static = {
        "status": status,
        "detail": detail,
        "session_id": (
            _text(data.get("session_id")).lower()
            if _DIGEST_RE.fullmatch(_text(data.get("session_id")).lower())
            else ""
        ),
        "plan_sha256": digest,
        "result_sha256": result_sha if _DIGEST_RE.fullmatch(result_sha) else "",
        "check_evidence": {
            "state": evidence_state,
            "checked_at": checked_at,
        },
        "recovery_action": recovery_action,
        "plan_url": plan_url,
    }
    recovery_next = (
        "Next: Retry restore. This only finishes restoring the previous state."
        if recovery_action == "restore"
        else "Next: Retry recovery. This only finishes restoring the previous state."
    )
    next_hidden = "" if recovery_action else " hidden"
    apply_hidden = "" if status in {"ready", "needs_decision"} else " hidden"
    recover_hidden = "" if recovery_action == "recover" else " hidden"
    restore_visible = status in {"complete", "adoption_required"} or recovery_action == "restore"
    restore_hidden = "" if restore_visible else " hidden"
    restore_label = "Retry restore" if recovery_action == "restore" else "Restore previous state"
    apply_check_finished = (
        status in {"complete", "adoption_required"}
        and evidence_state == "apply_time"
    )
    check_time = (
        checked_at
        if checked_at
        else "Time not recorded (older session)"
        if apply_check_finished
        else "Not run"
    )
    check_scope = (
        "This check ran when Apply finished. Later project changes are not included."
        if apply_check_finished
        else "The Apply-time check has not finished."
    )
    kit_files_result = "Passed at Apply" if apply_check_finished else "Not checked"
    protection_result = (
        "Passed at Apply"
        if status == "complete" and apply_check_finished
        else "Problems recorded at Apply"
        if status == "adoption_required" and apply_check_finished
        else "Not checked"
    )
    handoff_hidden = "" if status in {"complete", "adoption_required"} else " hidden"

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(title)}</title>
<style>{board_client.CSS}{_styles()}</style>
</head>
<body class="kit-change-page" data-kit-change-mode="{mode}" data-kit-change-status="{status}">
<main>
  <header>
    <p class="eyebrow">Kit change</p>
    <h1>{_esc(title)}</h1>
    <p class="subtitle">Review what changes, what stays safe, and how recovery works.</p>
    <p class="subtitle">This review server runs only on this computer. Stop it with <code>kit serve stop</code>.</p>
  </header>
  {_step_html(status)}
  {board_client.shell_html()}
  <section id="kit-change-file-guidance" class="connection-guidance" aria-live="polite">
    <h2>Review only — the cockpit is not connected</h2>
    <p>Open {link_copy}, or ask your agent to reopen this kit review.</p>
  </section>
  <section id="kit-change-down-guidance" class="connection-guidance" aria-live="polite">
    <h2>The cockpit is not reachable</h2>
    <p>No action is available. Ask your agent to reopen this kit review.</p>
    <button type="button" class="retry" id="kit-change-retry">Retry now</button>
  </section>
  <noscript>
    <style>.interactive-actions{{display:none!important}}</style>
    <section class="connection-guidance" style="display:block">
      <h2>Review only — JavaScript is unavailable</h2>
      <p>The complete review is shown below. {_esc(no_script_decision)}</p>
      <p class="fingerprint">Exact change fingerprint:<code>{_esc(digest, "Missing")}</code></p>
    </section>
  </noscript>
  <section class="status-card" role="status" aria-live="polite" aria-atomic="true">
    <p class="status-label" id="kit-change-status-label">{_esc(status_label)}</p>
    <p class="status-note" id="kit-change-status-note">{_esc(detail)}</p>
    <p class="status-next" id="kit-change-next-step"{next_hidden}>{_esc(recovery_next)}</p>
  </section>
  {blocker_html}
  <section class="section" aria-labelledby="quick-read-title">
    <h2 id="quick-read-title">Quick read</h2>
    <dl class="facts">
      <div><dt>Project</dt><dd>{_esc(project_name)}</dd></div>
      <div><dt>Kit version</dt><dd>{_esc(current_version)} → {_esc(incoming_version)}</dd></div>
      <div><dt>Game files</dt><dd class="{game_class}">{_esc(game_label)}</dd></div>
      <div><dt>Recovery</dt><dd>{_esc(recovery)}</dd></div>
      <div><dt>Design</dt><dd>{_esc(design)}</dd></div>
      <div><dt>Existing problems</dt><dd id="kit-change-gap-count">{_esc(gaps_label)}</dd></div>
    </dl>
  </section>
  {_decision_html(decisions)}
  <section class="section" aria-labelledby="changes-title">
    <h2 id="changes-title">What will change</h2>
    <ul class="counts">{counts_html}</ul>
    {_files_html(files)}
  </section>
  <section class="section" aria-labelledby="check-title">
    <h2 id="check-title">Apply-time check</h2>
    <p id="kit-change-check-scope">{_esc(check_scope)}</p>
    <dl class="check-list">
      <div><dt>When</dt><dd id="kit-change-checked-at">{_esc(check_time)}</dd></div>
      <div><dt>Kit files</dt><dd id="kit-change-kit-files-check">{_esc(kit_files_result)}</dd></div>
      <div><dt>New-work protection</dt><dd id="kit-change-new-work-check">{_esc(protection_result)}</dd></div>
      <div><dt>Godot check</dt><dd>Not run; separate approval required</dd></div>
    </dl>
    {gap_note}
    {problems_html}
    <p>For the current project, ask your agent to run <code>kit verify --static</code>.</p>
  </section>
  <section class="section provider-handoff" id="kit-change-provider-handoff"{handoff_hidden}>
    <h2>Before game work</h2>
    <p>Start a fresh Codex or Copilot chat. The current chat may still use the old kit rules.</p>
    <ul>
      <li>Codex: check the active instruction sources.</li>
      <li>Copilot: run <code>/instructions</code> and check References.</li>
    </ul>
  </section>
  <fieldset id="kit-change-controls" class="interactive-actions section" disabled>
    <legend class="eyebrow">Action</legend>
    <div class="actions">
      <button type="button" class="primary" id="kit-change-apply" data-board-control disabled{apply_hidden}>{_esc(action)}</button>
      <button type="button" class="primary" id="kit-change-recover" data-board-control disabled{recover_hidden}>Retry recovery</button>
      <button type="button" class="primary" id="kit-change-open-plan" data-board-control disabled hidden>Open project plan</button>
      <button type="button" class="secondary" id="kit-change-restore" data-board-control disabled{restore_hidden}>{_esc(restore_label)}</button>
      <button type="button" class="secondary" id="kit-change-review-again" data-board-control disabled hidden>Review again</button>
    </div>
  </fieldset>
  <details class="section technical">
    <summary>Technical details</summary>
    <p>Project folder: <code>{_esc(project_path)}</code></p>
    <p class="fingerprint">Exact change fingerprint:<code id="kit-change-plan-sha">{_esc(digest, "Missing")}</code></p>
  </details>
</main>
{board_client.core_js(review_url)}
{_client_script(static)}
</body>
</html>
"""


render_review = render
