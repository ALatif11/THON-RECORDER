"use strict";

// One guided flow: record -> write -> review -> post. History is off to the side.
const S = {
  server: null,
  view: "home",       // home | recording | working | draft | posted | history
  jobId: null,        // the job the "working" screen is following
  upload: null,       // {pct} while a file is uploading
  meeting: null,      // loaded detail for the draft screen
  advanced: false,    // opened from History: show transcript and notes tabs
  tab: "draft",       // draft | transcript | notes
  transcript: null,
  meetings: [],
  query: "",
  hits: [],
};

const $ = (id) => document.getElementById(id);
const esc = (s) =>
  String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );

async function api(path, opts) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (e) {}
    throw new Error(detail);
  }
  return res.json();
}

let toastTimer;
function toast(msg) {
  const el = $("toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 3200);
}

const fmtTime = (secs) => {
  const s = Math.floor(secs || 0);
  const h = Math.floor(s / 3600);
  const mm = String(Math.floor((s % 3600) / 60)).padStart(2, "0");
  const ss = String(s % 60).padStart(2, "0");
  return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
};

const fmtWhen = (iso) => {
  if (!iso) return "";
  const d = new Date(iso.replace(" ", "T"));
  return isNaN(d) ? iso : d.toLocaleString([], { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
};

function go(view) {
  S.view = view;
  render();
  window.scrollTo(0, 0);
}

// ------------------------------------------------------------- polling

async function refreshState() {
  try {
    S.server = await api("/api/state");
  } catch (e) {
    return;
  }
  const rec = S.server.recording || {};

  if (rec.recording && S.view !== "recording") return go("recording");
  if (!rec.recording && S.view === "recording") return go("home");
  if (S.view === "recording") $("elapsed").textContent = fmtTime(rec.elapsed);
  if (S.view === "working") updateWorking();
}

function tick() {
  const busy = S.view === "recording" || S.view === "working";
  refreshState().finally(() => setTimeout(tick, busy ? 1000 : 4000));
}

// ------------------------------------------------------------- render

function render() {
  const step = { home: 1, recording: 1, working: 2, draft: 3, posted: 4 }[S.view];
  $("steps").hidden = !step;
  for (const li of $("steps").children) {
    const n = Number(li.dataset.step);
    li.className = n === step ? "active" : n < step ? "done" : "";
  }
  $("history-link").classList.toggle("active", S.view === "history");

  const main = $("main");
  ({
    home: renderHome,
    recording: renderRecording,
    working: renderWorking,
    draft: renderDraft,
    posted: renderPosted,
    history: renderHistory,
  })[S.view](main);
}

// ------------------------------------------------------------- step 1

function renderHome(main) {
  const caps = (S.server && S.server.capabilities) || {};
  const canRecord = caps.capture !== false;
  const last = S.meetings.find((m) => m.has_notes && !m.posted_at);

  main.innerHTML = `
    <section class="card center">
      <div class="consent">
        <span class="rec-dot"></span>
        <div>Before you start, tell the room:<br><b>&ldquo;This meeting is being recorded.&rdquo;</b></div>
      </div>

      <button class="record-btn" id="rec-btn" ${canRecord ? "" : "disabled"}>
        <span class="rec-icon"></span>Record
      </button>
      ${canRecord ? "" : `<p class="error">Recording isn't available on this computer. You can still drop in a recording below.<br><small>${esc(caps.capture_error || "")}</small></p>`}

      <label class="source">Recording from
        <select id="source">
          <option value="mic">Microphone</option>
          <option value="speakers">Computer audio (Zoom, Teams)</option>
        </select>
      </label>

      <div class="or"><span>or</span></div>

      <button class="dropzone" id="dropzone">
        <b>Drop a recording here</b>
        <span>or click to choose a file (audio or video)</span>
      </button>

      ${last ? `<p class="resume">You have an unposted draft from ${esc(fmtWhen(last.when))}. <a href="#" id="resume">Open it</a></p>` : ""}
    </section>`;

  $("rec-btn").onclick = startRecording;
  $("dropzone").onclick = () => $("file-input").click();
  try { $("source").value = localStorage.getItem("source") || "mic"; } catch (e) {}
  $("source").onchange = () => { try { localStorage.setItem("source", $("source").value); } catch (e) {} };
  if (last) $("resume").onclick = (e) => { e.preventDefault(); openMeeting(last.stem); };
}

function renderRecording(main) {
  const rec = (S.server && S.server.recording) || {};
  main.innerHTML = `
    <section class="card center">
      <div class="live-banner"><span class="rec-dot pulse"></span>This meeting is being recorded</div>
      <div class="big-timer" id="elapsed">${fmtTime(rec.elapsed)}</div>
      <button class="stop-btn" id="stop-btn"><span class="stop-icon"></span>Stop &amp; write the announcement</button>
      <p class="sub">Recording from ${esc(rec.device || "the microphone")}. Keep this window open.</p>
    </section>`;
  $("stop-btn").onclick = stopRecording;
}

async function startRecording() {
  const btn = $("rec-btn");
  btn.disabled = true;
  try {
    await api("/api/record/start", {
      method: "POST",
      body: JSON.stringify({ label: "meeting", source: $("source").value }),
    });
    await refreshState();
  } catch (e) {
    toast(e.message);
    btn.disabled = false;
  }
}

async function stopRecording() {
  const btn = $("stop-btn");
  btn.disabled = true;
  btn.textContent = "Stopping...";
  try {
    const res = await api("/api/record/stop?process=true", { method: "POST" });
    if (res.error) toast("The recording stopped early: " + res.error);
    if (res.job) {
      S.jobId = res.job.id;
      go("working");
    } else {
      toast("Nothing was recorded.");
      go("home");
    }
  } catch (e) {
    toast(e.message);
    btn.disabled = false;
  }
}

// ------------------------------------------------------------- upload

function uploadFile(file) {
  if (!file) return;
  S.upload = { pct: 0 };
  go("working");

  const form = new FormData();
  form.append("files", file, file.name);
  form.append("label", "meeting");
  form.append("auto_queue", "true");

  // fetch() gives no upload progress, so this one call uses XHR.
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/upload");
  xhr.upload.onprogress = (e) => {
    if (e.lengthComputable) {
      S.upload.pct = Math.round((e.loaded / e.total) * 100);
      updateWorking();
    }
  };
  xhr.onload = () => {
    S.upload = null;
    let body = {};
    try { body = JSON.parse(xhr.responseText); } catch (err) {}
    if (xhr.status >= 200 && xhr.status < 300 && body.accepted && body.accepted.length) {
      S.jobId = body.accepted[0].job.id;
      updateWorking();
    } else {
      showFailure(body.detail || "That file couldn't be uploaded.", null);
    }
  };
  xhr.onerror = () => {
    S.upload = null;
    showFailure("The upload failed. Try again.", null);
  };
  xhr.send(form);
  $("file-input").value = "";
}

function wireDrop() {
  $("file-input").onchange = (e) => uploadFile(e.target.files[0]);

  const isFiles = (e) => Array.from(e.dataTransfer?.types || []).includes("Files");
  let depth = 0;
  window.addEventListener("dragenter", (e) => {
    if (!isFiles(e) || S.view !== "home") return;
    depth++;
    document.body.classList.add("dragging");
  });
  window.addEventListener("dragleave", () => {
    depth = Math.max(0, depth - 1);
    if (!depth) document.body.classList.remove("dragging");
  });
  window.addEventListener("dragover", (e) => { if (isFiles(e)) e.preventDefault(); });
  window.addEventListener("drop", (e) => {
    if (!isFiles(e)) return;
    e.preventDefault();
    depth = 0;
    document.body.classList.remove("dragging");
    if (S.view === "home") uploadFile(e.dataTransfer.files[0]);
  });
}

// ------------------------------------------------------------- step 2

function renderWorking(main) {
  main.innerHTML = `
    <section class="card center" id="working">
      <div class="spinner"></div>
      <h2 id="work-title">Getting started...</h2>
      <div class="bar"><div id="work-bar" style="width:0%"></div></div>
      <p class="sub">A long meeting can take a while. You can leave this window open and come back.</p>
    </section>`;
  updateWorking();
}

function updateWorking() {
  if (S.view !== "working" || !$("work-title")) return;

  if (S.upload) return setWorking("Uploading your recording...", S.upload.pct);

  const job = ((S.server && S.server.jobs) || []).find((j) => j.id === S.jobId);
  if (!job) return setWorking("Getting started...", 0);

  if (job.status === "failed" || job.status === "cancelled") return showFailure(job.error || "It was cancelled.", job);

  if (job.kind === "ingest") {
    if (job.status === "done" && job.result && job.result.queued_job) {
      S.jobId = job.result.queued_job;
      return updateWorking();
    }
    return setWorking("Getting the audio ready...", job.pct);
  }

  if (job.status === "done") {
    S.jobId = null; // so the next poll doesn't open it a second time
    return openMeeting(job.stem);
  }
  if (job.status === "queued") return setWorking("Getting ready...", 0);
  // The pipeline gives transcription the first 70% of the bar.
  setWorking(job.pct < 70 ? "Transcribing the meeting..." : "Writing your announcement...", job.pct);
}

function setWorking(title, pct) {
  $("work-title").textContent = title;
  $("work-bar").style.width = `${pct || 0}%`;
}

function showFailure(raw, job) {
  const ollama = /ollama/i.test(raw);
  const friendly = ollama
    ? "The writing assistant (Ollama) isn't running. Open the Ollama app, then press Try again. Your recording is saved, so it won't need to be transcribed again."
    : "Something went wrong while working on this recording.";
  $("main").innerHTML = `
    <section class="card center">
      <h2>That didn't work</h2>
      <p class="error">${esc(friendly)}</p>
      <details><summary>Details</summary><pre>${esc(raw)}</pre></details>
      <p class="actions">
        ${job && job.kind === "process" ? `<button class="primary" id="retry">Try again</button>` : ""}
        <button id="start-over">Start over</button>
      </p>
    </section>`;
  $("start-over").onclick = () => go("home");
  if ($("retry")) $("retry").onclick = () => processMeeting(job.stem);
}

async function processMeeting(stem) {
  try {
    const job = await api("/api/jobs", { method: "POST", body: JSON.stringify({ stem }) });
    S.jobId = job.id;
    await refreshState();
    go("working");
  } catch (e) {
    toast(e.message);
  }
}

// ------------------------------------------------------------- step 3 + 4

async function openMeeting(stem, advanced = false, tab = "draft") {
  try {
    S.meeting = await api(`/api/lectures/${encodeURIComponent(stem)}`);
  } catch (e) {
    return toast(e.message);
  }
  S.advanced = advanced;
  S.tab = tab;
  S.transcript = null;
  if (tab === "transcript") await loadTranscript();
  loadMeetings();
  go("draft");
}

async function loadTranscript() {
  try {
    S.transcript = (await api(`/api/lectures/${encodeURIComponent(S.meeting.meta.stem)}/transcript`)).segments;
  } catch (e) {
    S.transcript = [];
  }
}

const stemUrl = () => `/api/lectures/${encodeURIComponent(S.meeting.meta.stem)}`;

function renderDraft(main) {
  const meta = S.meeting.meta;
  const tabs = S.advanced
    ? `<div class="tabs">${[["draft", "Announcement"], ["transcript", "Transcript"], ["notes", "Notes"]]
        .map(([id, name]) => `<button class="tab ${S.tab === id ? "active" : ""}" data-tab="${id}">${name}</button>`)
        .join("")}</div>`
    : "";

  let body;
  if (S.tab === "transcript") body = transcriptHtml();
  else if (S.tab === "notes") body = notesHtml();
  else if (S.meeting.markdown == null) body = notProcessedHtml(meta);
  else body = draftHtml(meta);

  main.innerHTML = `
    <section class="card">
      <div class="draft-head">
        <div>
          <h1>${S.advanced ? esc(meta.title) : "Your announcement"}</h1>
          <div class="sub">${esc(fmtWhen(meta.when))}</div>
        </div>
        <span class="save-state" id="save-state"></span>
      </div>
      ${tabs}
      ${body}
    </section>`;

  for (const el of main.querySelectorAll(".tab")) {
    el.onclick = async () => {
      S.tab = el.dataset.tab;
      if (S.tab === "transcript" && !S.transcript) await loadTranscript();
      render();
    };
  }
  if ($("process-now")) $("process-now").onclick = () => processMeeting(meta.stem);
  if ($("draft")) wireDraft();
}

function notProcessedHtml(meta) {
  return meta.has_audio
    ? `<div class="empty"><p>This meeting hasn't been turned into an announcement yet.</p>
         <button class="primary" id="process-now">Write the announcement</button></div>`
    : `<div class="empty"><p>There's no recording or announcement for this one.</p></div>`;
}

function draftHtml(meta) {
  return `
    <p class="sub">Read it over and fix anything that's off. Nothing is posted until you press <b>Post to GroupMe</b>.</p>
    <div class="flags" id="flags"></div>
    <div class="editor">
      <div class="backdrop" id="backdrop" aria-hidden="true"></div>
      <textarea id="draft" spellcheck="true" aria-label="Announcement draft"></textarea>
    </div>

    <div class="rewrite">
      <span>Not quite right?</span>
      <select id="tone" aria-label="Style for the rewrite">
        <option value="">Same style</option>
        <option value="shorter">Shorter</option>
        <option value="hype">More hype</option>
        <option value="formal">More formal</option>
      </select>
      <button id="regen">Rewrite it</button>
    </div>

    <div class="post-row">
      <button class="primary big" id="post-btn">Post to GroupMe</button>
      <button id="copy-btn">Copy</button>
      ${meta.posted_at ? `<span class="posted-note">Posted ${esc(fmtWhen(meta.posted_at))}</span>` : ""}
    </div>`;
}

function highlightFlags(text) {
  // Mark the guessed word together with its [?], so it's easy to spot.
  return esc(text).replace(/(\S*\s?\[\?\])/g, "<mark>$1</mark>") + "\n";
}

function wireDraft() {
  const box = $("draft");
  box.value = S.meeting.markdown;

  const sync = () => {
    $("backdrop").innerHTML = highlightFlags(box.value);
    box.style.height = "auto";
    box.style.height = box.scrollHeight + 2 + "px";
    const n = (box.value.match(/\[\?\]/g) || []).length;
    $("flags").className = "flags " + (n ? "warn" : "ok");
    $("flags").innerHTML = n
      ? `<b>${n} spot${n === 1 ? "" : "s"} to double-check.</b> The assistant wasn't sure about the words highlighted in yellow. Fix them, then delete the [?].`
      : "Nothing flagged. Still give it a quick read.";
  };
  sync();

  let timer;
  box.oninput = () => {
    sync();
    $("save-state").textContent = "Saving...";
    clearTimeout(timer);
    timer = setTimeout(saveDraft, 700);
  };

  $("regen").onclick = regenerate;
  $("copy-btn").onclick = copyDraft;
  $("post-btn").onclick = openPost;
}

async function saveDraft() {
  const text = $("draft").value;
  try {
    await api(`${stemUrl()}/notes`, { method: "PUT", body: JSON.stringify({ markdown: text }) });
    S.meeting.markdown = text;
    if ($("save-state")) $("save-state").textContent = "Saved";
  } catch (e) {
    if ($("save-state")) $("save-state").textContent = "Not saved";
    toast("Couldn't save: " + e.message);
  }
}

async function regenerate() {
  if (!confirm("Replace this draft with a new version? Your edits to it will be lost.")) return;
  const btn = $("regen");
  btn.disabled = true;
  btn.textContent = "Rewriting...";
  document.querySelector(".editor").classList.add("busy");
  try {
    const res = await api(`${stemUrl()}/regenerate`, {
      method: "POST",
      body: JSON.stringify({ tone: $("tone").value }),
    });
    S.meeting.markdown = res.markdown;
    const tone = $("tone").value;
    render();
    $("tone").value = tone;
    toast("Here's a new version.");
  } catch (e) {
    toast(e.message);
    btn.disabled = false;
    btn.textContent = "Rewrite it";
    document.querySelector(".editor").classList.remove("busy");
  }
}

async function copyDraft() {
  const box = $("draft");
  await saveDraft();
  try {
    await navigator.clipboard.writeText(box.value);
  } catch (e) {
    box.select();
    document.execCommand("copy");
  }
  toast("Copied. You can paste it into the group chat.");
}

// The confirmation shows exactly what the server will send, split and all.
async function openPost() {
  await saveDraft();
  const confirmBtn = $("post-confirm");
  $("post-error").hidden = true;
  $("post-preview").innerHTML = "";
  $("post-sub").textContent = "";
  confirmBtn.hidden = false;
  confirmBtn.disabled = false;
  confirmBtn.textContent = "Post now";
  confirmBtn.onclick = confirmPost;
  $("post-modal").hidden = false;

  try {
    const res = await api(`${stemUrl()}/post?dry_run=true`, {
      method: "POST",
      body: JSON.stringify({ markdown: $("draft").value }),
    });
    const n = res.messages.length;
    $("post-sub").innerHTML =
      (n > 1 ? `GroupMe allows 1000 characters per message, so this will go out as <b>${n} messages</b>. ` : "This is exactly what your group will see. ") +
      (res.posted_at ? `<br><b class="warn-text">Heads up: this meeting was already posted ${esc(fmtWhen(res.posted_at))}.</b>` : "");
    $("post-preview").innerHTML = res.messages
      .map((m, i) => `<div class="bubble">${n > 1 ? `<div class="bubble-n">Message ${i + 1} of ${n}</div>` : ""}${esc(m)}</div>`)
      .join("");
  } catch (e) {
    showPostError(e.message);
    if (/bot id/i.test(e.message)) {
      confirmBtn.textContent = "Open Settings";
      confirmBtn.onclick = () => { closeModal("post-modal"); openSettings(); };
    } else {
      confirmBtn.hidden = true;
    }
  }
}

async function confirmPost() {
  const btn = $("post-confirm");
  btn.disabled = true;
  btn.textContent = "Posting...";
  $("post-error").hidden = true;
  try {
    await api(`${stemUrl()}/post`, { method: "POST", body: JSON.stringify({ markdown: $("draft").value }) });
    closeModal("post-modal");
    S.meeting.meta.posted_at = new Date().toISOString();
    go("posted");
  } catch (e) {
    showPostError(e.message);
    // If part of it already went out, a retry would post that part twice.
    btn.hidden = /^Posted \d/.test(e.message);
    btn.disabled = false;
    btn.textContent = "Try again";
  }
}

function showPostError(msg) {
  $("post-error").textContent = msg;
  $("post-error").hidden = false;
}

function renderPosted(main) {
  main.innerHTML = `
    <section class="card center">
      <div class="done-mark">&#10003;</div>
      <h2>Posted!</h2>
      <p class="sub">Your announcement is in the GroupMe chat.</p>
      <p class="actions">
        <button class="primary" id="again">Record another meeting</button>
        <button id="back">Back to the announcement</button>
      </p>
    </section>`;
  $("again").onclick = () => go("home");
  $("back").onclick = () => go("draft");
}

// ------------------------------------------------------------- history

async function loadMeetings() {
  try {
    S.meetings = (await api("/api/lectures")).lectures;
  } catch (e) {}
}

function statusWords(m) {
  if (m.posted_at) return ["Posted", "good"];
  if (m.has_notes) return ["Draft ready", "warn"];
  if (m.has_transcript) return ["Transcribed", ""];
  return ["Not processed", ""];
}

function renderHistory(main) {
  main.innerHTML = `
    <section class="card">
      <div class="draft-head"><h1>Past meetings</h1>
        <button id="history-back">Back</button></div>
      <input type="search" id="search" placeholder="Search every meeting..." value="${esc(S.query)}">
      <div id="history-list"></div>
    </section>`;
  $("history-back").onclick = () => go("home");
  let timer;
  $("search").oninput = () => {
    clearTimeout(timer);
    timer = setTimeout(runSearch, 220);
  };
  renderHistoryList();
  loadMeetings().then(renderHistoryList);
}

async function runSearch() {
  S.query = $("search").value.trim();
  S.hits = [];
  if (S.query) {
    try { S.hits = (await api(`/api/search?q=${encodeURIComponent(S.query)}`)).hits; } catch (e) {}
  }
  renderHistoryList();
}

function renderHistoryList() {
  const list = $("history-list");
  if (!list) return;

  if (S.query) {
    list.innerHTML = S.hits.length
      ? S.hits.map((h, i) => `
          <button class="row-item" data-i="${i}">
            <span class="row-main">${highlightTerms(h.snippet)}</span>
            <span class="row-sub">${esc(h.lecture.label)} &middot; ${esc(fmtWhen(h.lecture.when))}${h.timestamp ? ` &middot; ${esc(h.timestamp)}` : ""}</span>
          </button>`).join("")
      : `<p class="empty">Nothing matches &ldquo;${esc(S.query)}&rdquo;.</p>`;
    for (const el of list.querySelectorAll(".row-item")) {
      const h = S.hits[el.dataset.i];
      el.onclick = () => openMeeting(h.lecture.stem, true, h.source === "transcript" ? "transcript" : "draft");
    }
    return;
  }

  list.innerHTML = S.meetings.length
    ? S.meetings.map((m) => {
        const [word, cls] = statusWords(m);
        return `<button class="row-item" data-stem="${esc(m.stem)}">
          <span class="row-main">${esc(fmtWhen(m.when) || m.title)}</span>
          <span class="row-sub">${esc(m.label)} <span class="pill ${cls}">${word}</span></span>
        </button>`;
      }).join("")
    : `<p class="empty">No meetings yet.</p>`;
  for (const el of list.querySelectorAll(".row-item")) el.onclick = () => openMeeting(el.dataset.stem, true);
}

function highlightTerms(text) {
  let out = esc(text);
  for (const t of S.query.split(/\s+/).filter(Boolean)) {
    const re = new RegExp("(" + esc(t).replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + ")", "gi");
    out = out.replace(re, "<mark>$1</mark>");
  }
  return out;
}

function transcriptHtml() {
  if (!S.transcript || !S.transcript.length) return '<p class="empty">No transcript for this meeting.</p>';
  return S.transcript
    .map((s) => `<div class="seg"><span class="ts">${fmtTime(s.start)}</span><span>${esc(s.text)}</span></div>`)
    .join("");
}

function notesHtml() {
  const chunks = [].concat(S.meeting.structured || []);
  const merged = {};
  for (const c of chunks) {
    if (!c || typeof c !== "object") continue;
    for (const [k, v] of Object.entries(c)) if (Array.isArray(v)) (merged[k] = merged[k] || []).push(...v);
  }
  const sections = {
    asks: ["What members were asked to do", (it) => it.ask],
    events: ["Events", (it) => [it.name, it.date, it.time, it.location].filter(Boolean).join(" · ")],
    action_items: ["Action items", (it) => [it.task, it.owner && `(${it.owner})`, it.due && `due ${it.due}`].filter(Boolean).join(" ")],
    decisions: ["Decisions", (it) => it.decision],
    updates: ["Updates", (it) => it.point],
  };
  let out = "";
  for (const [key, [title, line]] of Object.entries(sections)) {
    const items = (merged[key] || []).filter((it) => it && typeof it === "object");
    if (!items.length) continue;
    out += `<h3>${title}</h3><ul class="notes-list">${items
      .map((it) => `<li>${it.timestamp ? `<span class="ts">${esc(it.timestamp)}</span>` : ""}${esc(line(it))}</li>`)
      .join("")}</ul>`;
  }
  return out || '<p class="empty">No notes were extracted for this meeting.</p>';
}

// ------------------------------------------------------------- settings

async function openSettings() {
  $("settings-error").hidden = true;
  $("bot-id").value = "";
  $("settings-modal").hidden = false;
  try {
    const s = await api("/api/settings");
    $("bot-id").placeholder = s.bot_id_set ? "Saved. Paste a new one to replace it." : "Paste the Bot ID here";
    $("bot-id-status").textContent = !s.bot_id_set
      ? "Not set yet. Posting won't work until you add it."
      : s.bot_id_source === "environment"
        ? `Using the ID ending in ${s.bot_id_hint.replace("...", "")} from the GROUPME_BOT_ID environment variable. It takes priority over this box.`
        : `Saved (ends in ${s.bot_id_hint.replace("...", "")}).`;
    $("examples").value = s.examples;
  } catch (e) {
    $("settings-error").textContent = e.message;
    $("settings-error").hidden = false;
  }
  $("bot-id").focus();
}

async function saveSettings() {
  try {
    await api("/api/settings", {
      method: "PUT",
      body: JSON.stringify({ bot_id: $("bot-id").value.trim() || null, examples: $("examples").value }),
    });
    closeModal("settings-modal");
    toast("Settings saved");
  } catch (e) {
    $("settings-error").textContent = e.message;
    $("settings-error").hidden = false;
  }
}

function closeModal(id) {
  $(id).hidden = true;
}

// ------------------------------------------------------------- boot

async function boot() {
  $("home-link").onclick = (e) => { e.preventDefault(); if (S.view !== "recording" && S.view !== "working") go("home"); };
  $("history-link").onclick = (e) => { e.preventDefault(); if (S.view !== "recording") go("history"); };
  $("settings-link").onclick = (e) => { e.preventDefault(); openSettings(); };
  $("settings-save").onclick = saveSettings;
  $("settings-cancel").onclick = () => closeModal("settings-modal");
  $("post-cancel").onclick = () => closeModal("post-modal");
  for (const id of ["settings-modal", "post-modal"]) {
    $(id).onclick = (e) => { if (e.target === $(id)) closeModal(id); };
  }
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") ["settings-modal", "post-modal"].forEach(closeModal);
  });
  wireDrop();

  await Promise.all([refreshState(), loadMeetings()]);

  // Pick up where things were left: a live recording, or a job still running.
  const active = ((S.server && S.server.jobs) || []).filter((j) => j.status === "running" || j.status === "queued");
  if (S.view !== "recording" && active.length) {
    S.jobId = active[active.length - 1].id;
    S.view = "working";
  }
  render();
  tick();
}

boot();
