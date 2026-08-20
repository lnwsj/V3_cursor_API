"""HTML page templates (Phase 2.X refactor).

Each template is the raw HTML body returned by a route handler.
Templates are large (7-21KB each) and rarely change — keeping them in
their own module keeps main.py focused on route logic.
"""


# (17661 bytes)
_PUBLIC_SUBMIT_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Submit Job · V3 Studio</title>
<style>
*,*::before,*::after{box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
background:linear-gradient(180deg,#0e1320 0%,#0a0c14 100%);color:#e8e8f0;margin:0;min-height:100vh;font-size:14px}
.wrap{max-width:980px;margin:0 auto;padding:24px 20px 60px}
header{display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #1f2533;padding-bottom:18px;margin-bottom:28px}
.brand{display:flex;align-items:center;gap:12px}
.brand-mark{width:42px;height:42px;border-radius:11px;background:linear-gradient(135deg,#22c55e 0%,#10b981 60%,#06b6d4 100%);display:flex;align-items:center;justify-content:center;font-size:21px}
h1{margin:0;font-size:22px;font-weight:600}
.tagline{margin:3px 0 0;font-size:12px;color:#9aa0b4}
.user-menu{display:flex;align-items:center;gap:14px;font-size:13px}
.user-menu a{color:#60a5fa;text-decoration:none;padding:6px 12px;border-radius:6px}
.user-menu a:hover{background:#252837;color:#e8e8f0}
.card{background:rgba(20,24,34,0.7);border:1px solid #252837;border-radius:14px;padding:24px;margin-bottom:20px}
.tc-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:20px}
.tc-tile{padding:14px 12px;border:1px solid #252837;border-radius:10px;background:#0e1320;cursor:pointer;text-align:center;transition:all 0.2s;position:relative}
.tc-tile:hover{border-color:#22c55e;background:#141822}
.tc-tile.active{border-color:#22c55e;background:linear-gradient(135deg,rgba(34,197,94,0.15),rgba(16,185,129,0.1));box-shadow:0 0 0 1px #22c55e}
.tc-tile .name{font-weight:600;font-size:14px}
.tc-tile .desc{font-size:11px;color:#9aa0b4;margin-top:3px}
.tc-tile .badge{position:absolute;top:6px;right:8px;background:#252837;font-size:9px;padding:2px 6px;border-radius:3px;text-transform:uppercase;color:#fbbf24;font-weight:600}
.field{margin-bottom:16px}
.field label{display:block;font-size:12px;color:#9aa0b4;margin-bottom:6px;font-weight:500;text-transform:uppercase;letter-spacing:0.04em}
.field input[type=text],.field input[type=number],.field input[type=color],.field select,.field textarea{width:100%;padding:10px 12px;background:#0e1320;border:1px solid #252837;border-radius:7px;color:#e8e8f0;font-size:14px;font-family:inherit}
.field input[type=color]{height:42px;padding:4px}
.field input:focus,.field select:focus,.field textarea:focus{outline:none;border-color:#22c55e;background:#141822}
.field textarea{min-height:60px;resize:vertical}
.drop{border:2px dashed #252837;border-radius:10px;padding:24px;text-align:center;cursor:pointer;transition:all 0.2s;background:#0e1320}
.drop:hover,.drop.dragover{border-color:#22c55e;background:#141822}
.drop .hint{font-size:12px;color:#9aa0b4;margin-top:6px}
.drop input[type=file]{display:none}
.drop .filename{margin-top:8px;font-size:12px;color:#22c55e;font-family:"SF Mono",Consolas,monospace}
.btn{display:inline-flex;align-items:center;gap:8px;padding:10px 18px;border-radius:8px;border:none;cursor:pointer;font-family:inherit;font-weight:500;font-size:14px}
.btn-primary{background:linear-gradient(135deg,#22c55e,#10b981);color:#0a0c14}
.btn-primary:hover{transform:translateY(-1px);box-shadow:0 6px 18px rgba(34,197,94,0.25)}
.btn-primary:disabled{opacity:0.5;cursor:not-allowed;transform:none}
.btn-secondary{background:#252837;color:#e8e8f0}
.btn-secondary:hover{background:#2f3548}
.row{display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px}
@media(max-width:720px){.row{grid-template-columns:1fr}}
.progress-overlay{position:fixed;inset:0;background:rgba(10,12,20,0.85);backdrop-filter:blur(4px);display:none;align-items:center;justify-content:center;z-index:100}
.progress-overlay.active{display:flex}
.progress-box{background:#141822;border:1px solid #252837;border-radius:14px;padding:32px;max-width:440px;width:90%;text-align:center}
.spinner{display:inline-block;width:36px;height:36px;border:3px solid #252837;border-top-color:#22c55e;border-radius:50%;animation:spin 0.8s linear infinite;margin-bottom:14px}
@keyframes spin{to{transform:rotate(360deg)}}
.progress-box h3{margin:0 0 10px 0;font-size:18px}
.progress-box .step{color:#9aa0b4;font-size:13px;margin:6px 0}
.progress-box .step.done{color:#22c55e}
.progress-box .step.active{color:#fbbf24;font-weight:600}
.success{background:rgba(34,197,94,0.15);color:#86efac;padding:12px 16px;border-radius:8px;border:1px solid rgba(34,197,94,0.3);font-size:13px;margin-bottom:14px}
.error{background:rgba(239,68,68,0.15);color:#fca5a5;padding:12px 16px;border-radius:8px;border:1px solid rgba(239,68,68,0.3);font-size:13px;margin-bottom:14px}
.muted{color:#9aa0b4}
.row-2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:600px){.row-2{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="wrap" id="root">Loading…</div>
<div class="progress-overlay" id="overlay">
  <div class="progress-box">
    <div class="spinner" id="spinner"></div>
    <h3 id="overlayTitle">Submitting job…</h3>
    <div class="step" id="stepUpload">⏵ Upload files</div>
    <div class="step" id="stepDispatch">⏵ Dispatch to worker</div>
    <div class="step" id="stepDone">⏵ Done</div>
  </div>
</div>
<script>
const API = "";

const TC_DEFS = {
  tc01: {name: "TC01", desc: "Chroma key (single product + bg)", fields: ["product","background"]},
  tc02: {name: "TC02", desc: "Reframe + chroma (7×3 = 21 outputs)", fields: ["product","background"]},
  tc03: {name: "TC03", desc: "Batch segments + chroma", fields: ["product","background","audio"]},
  tc04: {name: "TC04", desc: "Reframe + batch + chroma", fields: ["product","background"]},
  tc05: {name: "TC05", desc: "Reframe-only (multi sources)", fields: ["sources"]},
  tc06: {name: "TC06", desc: "Chroma + audio master (folder)", fields: []},
};

const DEFAULTS = {
  width: 1080, height: 1920, fps: 30, bitrate: "6000k",
  key_color: "#00FF00", similarity: 0.29, blend: 0.04, despill: 0.32,
  encoder: "nvenc", preset: "medium",
};

let selectedTC = "tc02";
let productFile = null, bgFile = null, audioFile = null, sourceFiles = [];

async function api(method, url, body, isForm=false) {
  const opts = { method, headers: {}, credentials: "same-origin" };
  if (body && !isForm) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  if (body && isForm) opts.body = body;
  const r = await fetch(API + url, opts);
  const text = await r.text();
  let d; try { d = JSON.parse(text); } catch { d = { ok:false, error: text }; }
  if (!r.ok) throw new Error(d.detail || d.error || r.statusText);
  return d;
}

function esc(s) { return String(s ?? "").replace(/[<>&"']/g, c => ({"<":"&lt;",">":"&gt;","&":"&amp;",'"':"&quot;","'":"&#39;"}[c])); }

async function load() {
  let me;
  try { me = await api("GET", "/api/v1/auth/me"); }
  catch { location.href = "/api/app?tab=login"; return; }
  render(me);
}

function render(me) {
  const root = document.getElementById("root");
  root.innerHTML = `
    <header>
      <a href="/api/app" class="brand" style="text-decoration:none;color:inherit">
        <div class="brand-mark">🎬</div><div><h1>Submit Job</h1><p class="tagline">${esc(me.user.email)} · ${me.user.monthly_used}/${me.user.monthly_quota} jobs used</p></div>
      </a>
      <div class="user-menu">
        <a href="/api/app/jobs">My Jobs</a><a href="/api/app/submit" style="background:linear-gradient(135deg,#22c55e,#10b981);color:#0a0c14;padding:6px 12px;border-radius:6px;text-decoration:none">+ Submit</a>
        <a href="/api/app">Home</a>
        <button onclick="logout()" style="background:none;border:none;color:#60a5fa;cursor:pointer;font-family:inherit;font-size:13px;padding:6px 12px">Logout</button>
      </div>
    </header>

    <div class="card">
      <h2 style="margin-top:0">1. Choose Pipeline</h2>
      <div class="tc-grid">
        ${Object.entries(TC_DEFS).map(((([k,v]) => `
          <div class="tc-tile ${selectedTC===k?"active":""}" onclick="selectTC('${k}')" id="tc-${k}">
            ${v.fields.length===0?'<div class="badge">web=no</div>':''}
            <div class="name">${v.name}</div>
            <div class="desc">${esc(v.desc)}</div>
          </div>`)).join(""))}
      </div>
    </div>

    <div class="card" id="uploadCard">
      <h2 style="margin-top:0">2. Upload Files</h2>
      <div id="fileFields"></div>
    </div>

    <div class="card">
      <h2 style="margin-top:0">3. Settings</h2>
      <div class="row">
        <div class="field"><label>Width</label><input type="number" id="width" value="${DEFAULTS.width}"></div>
        <div class="field"><label>Height</label><input type="number" id="height" value="${DEFAULTS.height}"></div>
        <div class="field"><label>FPS</label><input type="number" id="fps" value="${DEFAULTS.fps}"></div>
      </div>
      <div class="row-2" style="margin-top:14px">
        <div class="field"><label>Encoder</label>
          <select id="encoder">
            <option value="nvenc" ${DEFAULTS.encoder==='nvenc'?'selected':''}>h264_nvenc (NVIDIA GPU)</option>
            <option value="libx264" ${DEFAULTS.encoder==='libx264'?'selected':''}>libx264 (CPU)</option>
            <option value="h264_videotoolbox" ${DEFAULTS.encoder==='h264_videotoolbox'?'selected':''}>h264_videotoolbox (macOS)</option>
          </select>
        </div>
        <div class="field"><label>Preset</label>
          <select id="preset">
            <option value="medium" ${DEFAULTS.preset==='medium'?'selected':''}>medium (balanced)</option>
            <option value="slow" ${DEFAULTS.preset==='slow'?'selected':''}>slow (better quality)</option>
            <option value="fast" ${DEFAULTS.preset==='fast'?'selected':''}>fast (faster)</option>
            <option value="p4" ${DEFAULTS.preset==='p4'?'selected':''}>p4 (NVENC)</option>
            <option value="p5" ${DEFAULTS.preset==='p5'?'selected':''}>p5 (NVENC)</option>
          </select>
        </div>
      </div>
      <div class="row-2" style="margin-top:14px">
        <div class="field"><label>Bitrate</label><input type="text" id="bitrate" value="${DEFAULTS.bitrate}"></div>
        <div class="field"><label>Key color</label><input type="color" id="key_color" value="${DEFAULTS.key_color}"></div>
      </div>
      <div class="row-2" style="margin-top:14px">
        <div class="field"><label>Similarity</label><input type="number" id="similarity" step="0.01" value="${DEFAULTS.similarity}"></div>
        <div class="field"><label>Despill</label><input type="number" id="despill" step="0.01" value="${DEFAULTS.despill}"></div>
      </div>
    </div>

    <div class="card">
      <h2 style="margin-top:0">4. Submit</h2>
      <div id="submitMsg"></div>
      <button class="btn btn-primary" onclick="submitJob()" id="submitBtn">Submit Job</button>
      <a href="/api/app/jobs" class="btn btn-secondary" style="margin-left:8px">View Jobs</a>
    </div>
  `;
  renderFileFields();
}

function selectTC(tc) {
  if (TC_DEFS[tc].fields.length === 0) {
    alert(tc.toUpperCase() + " requires a folder structure (product_root). Not supported via web yet.");
    return;
  }
  selectedTC = tc;
  document.querySelectorAll(".tc-tile").forEach(el => el.classList.remove("active"));
  document.getElementById("tc-" + tc).classList.add("active");
  renderFileFields();
  productFile = null; bgFile = null; audioFile = null; sourceFiles = [];
}

function renderFileFields() {
  const fields = TC_DEFS[selectedTC].fields;
  const html = fields.map(f => {
    const label = {product:"Product (green screen video)", background:"Background video", audio:"Audio file (optional)", sources:"Source videos (multiple)"}[f];
    const multi = (f === "sources");
    return `<div class="field">
      <label>${esc(label)}</label>
      <div class="drop" onclick="document.getElementById('file-${f}').click()" ondragover="event.preventDefault();this.classList.add('dragover')" ondragleave="this.classList.remove('dragover')" ondrop="handleDrop(event, '${f}')">
        <input type="file" id="file-${f}" ${multi?'multiple':''} accept="video/*,audio/*" onchange="handleFile('${f}', this.files)">
        <div style="font-size:24px">📁</div>
        <div>Click to choose ${multi?'files':'file'} or drag here</div>
        <div class="hint">${multi?'Select multiple source videos':'MP4 / MOV / WAV supported'}</div>
        <div class="filename" id="fname-${f}"></div>
      </div>
    </div>`;
  }).join("");
  document.getElementById("fileFields").innerHTML = html;
}

function handleFile(role, files) {
  if (role === "product") { productFile = files[0]; document.getElementById("fname-product").textContent = productFile?.name || ""; }
  else if (role === "background") { bgFile = files[0]; document.getElementById("fname-background").textContent = bgFile?.name || ""; }
  else if (role === "audio") { audioFile = files[0]; document.getElementById("fname-audio").textContent = audioFile?.name || ""; }
  else if (role === "sources") { sourceFiles = Array.from(files); document.getElementById("fname-sources").textContent = sourceFiles.map(f=>f.name).join(", "); }
}
function handleDrop(ev, role) {
  ev.preventDefault();
  ev.currentTarget.classList.remove("dragover");
  handleFile(role, ev.dataTransfer.files);
}

async function logout() { await fetch("/api/v1/auth/logout", { method: "POST", credentials: "same-origin" }); location.href = "/api/app"; }

async function uploadToRole(role, file) {
  const fd = new FormData();
  fd.append("file", file);
  const r = await fetch("/api/v1/uploads/" + role, { method: "POST", body: fd, credentials: "same-origin" });
  const d = await r.json();
  if (!d.ok) throw new Error(d.detail || "upload failed");
  return d.file_id;
}

function step(id, status) {
  const el = document.getElementById(id);
  el.classList.remove("active", "done");
  if (status === "active") el.classList.add("active");
  if (status === "done") { el.classList.add("done"); el.textContent = el.textContent.replace("⏵", "✓"); }
  else if (status === "active") el.textContent = el.textContent.replace("⏵", "▶");
}

async function submitJob() {
  const msg = document.getElementById("submitMsg");
  msg.innerHTML = "";
  document.getElementById("submitBtn").disabled = true;
  document.getElementById("overlay").classList.add("active");
  document.getElementById("overlayTitle").textContent = "Submitting job…";
  ["stepUpload","stepDispatch","stepDone"].forEach(s => { document.getElementById(s).className = "step"; document.getElementById(s).textContent = document.getElementById(s).textContent.replace("✓","⏵").replace("▶","⏵"); });

  try {
    const fields = TC_DEFS[selectedTC].fields;
    const fileIds = {};
    step("stepUpload", "active");
    if (fields.includes("product")) {
      if (!productFile) throw new Error("Please choose a product file");
      fileIds.product_id = await uploadToRole("product", productFile);
    }
    if (fields.includes("background")) {
      if (!bgFile) throw new Error("Please choose a background file");
      fileIds.background_id = await uploadToRole("background", bgFile);
    }
    if (fields.includes("audio") && audioFile) {
      fileIds.audio_id = await uploadToRole("audio", audioFile);
    }
    if (fields.includes("sources")) {
      if (!sourceFiles.length) throw new Error("Please choose source files");
      fileIds.source_ids = [];
      for (const f of sourceFiles) fileIds.source_ids.push(await uploadToRole("source", f));
    }
    step("stepUpload", "done");

    step("stepDispatch", "active");
    const settings = {
      width: parseInt(document.getElementById("width").value),
      height: parseInt(document.getElementById("height").value),
      fps: parseInt(document.getElementById("fps").value),
      encoder: document.getElementById("encoder").value,
      preset: document.getElementById("preset").value,
      bitrate: document.getElementById("bitrate").value,
      key_color: document.getElementById("key_color").value,
      similarity: parseFloat(document.getElementById("similarity").value),
      blend: DEFAULTS.blend,
      despill: parseFloat(document.getElementById("despill").value),
    };
    // Build payload compatible with V3RenderPayload: { files: {role: [file_id]}, settings: {...} }
    const files = {};
    if (fileIds.product_id) files.product = [fileIds.product_id];
    if (fileIds.background_id) files.background = [fileIds.background_id];
    if (fileIds.audio_id) files.audio = [fileIds.audio_id];
    if (fileIds.source_ids) files.source = fileIds.source_ids;
    const payload = { mode: selectedTC, files, settings };
    const r = await fetch("/api/" + selectedTC + "/render", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload), credentials: "same-origin" });
    const d = await r.json();
    step("stepDispatch", "done");

    if (!d.ok) {
      step("stepDone", "active");
      document.getElementById("overlayTitle").textContent = "Failed";
      throw new Error(d.detail || d.message || "render failed");
    }

    step("stepDone", "done");
    document.getElementById("overlayTitle").textContent = "Job submitted!";
    setTimeout(() => {
      document.getElementById("overlay").classList.remove("active");
      window.location.href = "/api/app/job/" + d.job_id;
    }, 1500);
  } catch (e) {
    msg.innerHTML = `<div class="error">✕ ${esc(e.message)}</div>`;
    document.getElementById("overlay").classList.remove("active");
    document.getElementById("submitBtn").disabled = false;
  }
}

load();
</script>
</body>
</html>
"""


# (9044 bytes)
_PROFILE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Profile · V3 Studio</title>
<style>
*,*::before,*::after{box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
background:linear-gradient(180deg,#0e1320 0%,#0a0c14 100%);color:#e8e8f0;margin:0;min-height:100vh;font-size:14px}
.wrap{max-width:720px;margin:0 auto;padding:24px 20px 60px}
header{display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #1f2533;padding-bottom:18px;margin-bottom:28px}
.brand{display:flex;align-items:center;gap:12px}
.brand-mark{width:42px;height:42px;border-radius:11px;background:linear-gradient(135deg,#22c55e 0%,#10b981 60%,#06b6d4 100%);display:flex;align-items:center;justify-content:center;font-size:21px}
h1{margin:0;font-size:22px;font-weight:600}
.tagline{margin:3px 0 0;font-size:12px;color:#9aa0b4}
.user-menu{display:flex;align-items:center;gap:14px;font-size:13px}
.user-menu a{color:#60a5fa;text-decoration:none;padding:6px 12px;border-radius:6px}
.user-menu a:hover{background:#252837;color:#e8e8f0}
.card{background:rgba(20,24,34,0.7);border:1px solid #252837;border-radius:14px;padding:24px;margin-bottom:20px}
.card h2{margin:0 0 16px 0;font-size:13px;color:#9aa0b4;text-transform:uppercase;letter-spacing:0.06em;font-weight:600}
.field{margin-bottom:14px}
.field label{display:block;font-size:12px;color:#9aa0b4;margin-bottom:6px;font-weight:500}
.field input{width:100%;padding:10px 12px;background:#0e1320;border:1px solid #252837;border-radius:7px;color:#e8e8f0;font-size:14px;font-family:inherit}
.field input:focus{outline:none;border-color:#22c55e;background:#141822}
.btn{display:inline-flex;align-items:center;gap:8px;padding:10px 18px;border-radius:8px;border:none;cursor:pointer;font-family:inherit;font-weight:500;font-size:14px}
.btn-primary{background:linear-gradient(135deg,#22c55e,#10b981);color:#0a0c14}
.btn-primary:hover{transform:translateY(-1px);box-shadow:0 6px 18px rgba(34,197,94,0.25)}
.btn-secondary{background:#252837;color:#e8e8f0}
.btn-secondary:hover{background:#2f3548}
.btn-danger{background:rgba(239,68,68,0.2);color:#ef4444;border:1px solid rgba(239,68,68,0.3)}
.btn-danger:hover{background:rgba(239,68,68,0.3)}
.error{background:rgba(239,68,68,0.15);color:#fca5a5;padding:10px 14px;border-radius:8px;border:1px solid rgba(239,68,68,0.3);font-size:13px;margin-bottom:14px}
.success{background:rgba(34,197,94,0.15);color:#86efac;padding:10px 14px;border-radius:8px;border:1px solid rgba(34,197,94,0.3);font-size:13px;margin-bottom:14px}
.api-key-box{background:#0e1320;border:1px solid #252837;border-radius:8px;padding:14px;font-family:"SF Mono",Consolas,monospace;font-size:12px;color:#fbbf24;word-break:break-all;margin:8px 0}
.info-row{display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #1f2533}
.info-row:last-child{border-bottom:none}
.info-row .k{color:#9aa0b4;font-size:11pt}
.info-row .v{color:#e8e8f0;font-family:"SF Mono",Consolas,monospace;font-size:11pt}
.danger-zone{border:1px solid rgba(239,68,68,0.3);background:rgba(239,68,68,0.05)}
</style>
</head>
<body>
<div class="wrap" id="root">Loading…</div>
<script>
async function api(method, url, body) {
  const opts = { method, headers: {}, credentials: "same-origin" };
  if (body) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  const r = await fetch(url, opts);
  const text = await r.text();
  let d; try { d = JSON.parse(text); } catch { d = { ok:false, error: text }; }
  if (!r.ok) throw new Error(d.detail || d.error || r.statusText);
  return d;
}
function esc(s) { return String(s ?? "").replace(/[<>&"']/g, c => ({"<":"&lt;",">":"&gt;","&":"&amp;",'"':"&quot;","'":"&#39;"}[c])); }
function fmtTs(epoch) { if (!epoch) return "—"; return new Date(epoch * 1000).toLocaleString(); }

async function load() {
  let me;
  try { me = await api("GET", "/api/v1/auth/me"); }
  catch { location.href = "/api/app?tab=login"; return; }
  const u = me.user;
  render(u);
}

function render(u) {
  document.getElementById("root").innerHTML = `
    <header>
      <a href="/api/app" class="brand" style="text-decoration:none;color:inherit">
        <div class="brand-mark">🎬</div><div><h1>Profile</h1><p class="tagline">${esc(u.email)}</p></div>
      </a>
      <div class="user-menu">
        <a href="/api/app/jobs">Jobs</a>
        <a href="/api/app/submit">+ Submit Job</a>
        <button onclick="logout()" style="background:none;border:none;color:#60a5fa;cursor:pointer;font-family:inherit;font-size:13px;padding:6px 12px">Logout</button>
      </div>
    </header>

    <div class="card">
      <h2>Account Info</h2>
      <div id="profileErr"></div>
      <div id="profileOk"></div>
      <div class="info-row"><span class="k">User ID</span><span class="v">${esc(u.user_id)}</span></div>
      <div class="info-row"><span class="k">Email</span><span class="v">${esc(u.email || "—")}</span></div>
      <div class="info-row"><span class="k">Display name</span><span class="v">${esc(u.display_name || "—")}</span></div>
      <div class="info-row"><span class="k">Role</span><span class="v">${esc(u.role)}</span></div>
      <div class="info-row"><span class="k">Monthly quota</span><span class="v">${u.monthly_used} / ${u.monthly_quota}</span></div>
      <div class="info-row"><span class="k">API key prefix</span><span class="v">${esc(u.api_key_prefix || "—")}</span></div>
      <div class="info-row"><span class="k">Created</span><span class="v">${fmtTs(u.created_at)}</span></div>
      <div class="info-row"><span class="k">Last seen</span><span class="v">${fmtTs(u.last_seen_at)}</span></div>
    </div>

    <div class="card">
      <h2>Update Profile</h2>
      <form onsubmit="return updateProfile(event)">
        <div class="field"><label>Display name</label><input name="display_name" value="${esc(u.display_name || "")}"></div>
        <div class="field"><label>Email (must be unique)</label><input name="email" type="email" value="${esc(u.email || "")}"></div>
        <button class="btn btn-primary" type="submit">Save changes</button>
      </form>
    </div>

    <div class="card">
      <h2>Change Password</h2>
      <div id="pwErr"></div>
      <div id="pwOk"></div>
      <form onsubmit="return changePassword(event)">
        <div class="field"><label>Current password</label><input name="old_password" type="password" required></div>
        <div class="field"><label>New password (min 8 chars)</label><input name="new_password" type="password" required minlength="8"></div>
        <div class="field"><label>Confirm new password</label><input name="confirm" type="password" required minlength="8"></div>
        <button class="btn btn-primary" type="submit">Update password</button>
      </form>
    </div>

    <div class="card danger-zone">
      <h2 style="color:#ef4444">Danger Zone</h2>
      <p class="muted">Account-level actions.</p>
      <button class="btn btn-danger" onclick="confirmDelete()">Delete my account</button>
    </div>
  `;
}

async function updateProfile(e) {
  e.preventDefault();
  const err = document.getElementById("profileErr");
  const ok = document.getElementById("profileOk");
  err.innerHTML = ""; ok.innerHTML = "";
  const fd = new FormData(e.target);
  const body = {};
  if (fd.get("display_name")) body.display_name = fd.get("display_name");
  if (fd.get("email")) body.email = fd.get("email");
  if (!Object.keys(body).length) { err.textContent = "No changes"; err.classList.add("error"); return false; }
  try {
    await api("PATCH", "/api/v1/auth/me", body);
    ok.textContent = "✓ Saved";
    setTimeout(() => location.reload(), 1000);
  } catch (ex) {
    err.textContent = "✕ " + ex.message;
    err.classList.add("error");
  }
  return false;
}

async function changePassword(e) {
  e.preventDefault();
  const err = document.getElementById("pwErr");
  const ok = document.getElementById("pwOk");
  err.innerHTML = ""; ok.innerHTML = "";
  const fd = new FormData(e.target);
  const oldpw = fd.get("old_password");
  const newpw = fd.get("new_password");
  const confirm = fd.get("confirm");
  if (newpw !== confirm) {
    err.innerHTML = '<div class="error">✕ New passwords do not match</div>';
    return false;
  }
  try {
    await api("POST", "/api/v1/auth/change-password", { old_password: oldpw, new_password: newpw });
    ok.innerHTML = '<div class="success">✓ Password changed</div>';
    e.target.reset();
  } catch (ex) {
    err.innerHTML = '<div class="error">✕ ' + ex.message + '</div>';
  }
  return false;
}

async function confirmDelete() {
  if (!confirm("Are you sure? This will permanently delete your account and all jobs.")) return;
  // No endpoint yet — just warn
  alert("Account deletion requires contacting support@sj88ai.com. We'll handle it within 24h.");
}

async function logout() { await api("POST", "/api/v1/auth/logout", {}); location.href = "/api/app"; }

load();
</script>
</body>
</html>
"""


# (14459 bytes)
_APP_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>V3 Studio · Video Rendering</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
*,*::before,*::after{box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
background:linear-gradient(180deg,#0e1320 0%,#0a0c14 100%);color:#e8e8f0;margin:0;min-height:100vh;font-size:14px}
.wrap{max-width:1180px;margin:0 auto;padding:24px 20px 60px}
header{display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #1f2533;padding-bottom:18px;margin-bottom:28px;flex-wrap:wrap;gap:16px}
.brand{display:flex;align-items:center;gap:12px}
.brand-mark{width:42px;height:42px;border-radius:11px;background:linear-gradient(135deg,#22c55e 0%,#10b981 60%,#06b6d4 100%);
display:flex;align-items:center;justify-content:center;font-size:21px;box-shadow:0 4px 14px rgba(34,197,94,0.25)}
h1{margin:0;font-size:22px;font-weight:600;letter-spacing:-0.01em}
.tagline{margin:3px 0 0;font-size:12px;color:#9aa0b4}
.user-menu{display:flex;align-items:center;gap:14px;font-size:13px}
.user-menu a,.user-menu button{color:#60a5fa;text-decoration:none;background:none;border:none;cursor:pointer;font-family:inherit;font-size:13px;padding:6px 12px;border-radius:6px}
.user-menu a:hover,.user-menu button:hover{background:#252837;color:#e8e8f0}
.btn{display:inline-flex;align-items:center;gap:8px;padding:10px 18px;border-radius:8px;border:none;cursor:pointer;font-family:inherit;font-weight:500;font-size:14px;transition:all 0.2s;text-decoration:none}
.btn-primary{background:linear-gradient(135deg,#22c55e,#10b981);color:#0a0c14}
.btn-primary:hover{transform:translateY(-1px);box-shadow:0 6px 18px rgba(34,197,94,0.25)}
.btn-secondary{background:#252837;color:#e8e8f0}
.btn-secondary:hover{background:#2f3548}
.btn-ghost{background:transparent;color:#9aa0b4;border:1px solid #252837}
.btn-ghost:hover{background:#1a1d29;color:#e8e8f0}
.card{background:rgba(20,24,34,0.7);backdrop-filter:blur(8px);border:1px solid #252837;border-radius:14px;padding:22px;margin-bottom:20px}
.auth-card{max-width:480px;margin:60px auto}
.auth-card h2{margin:0 0 6px 0;font-size:20px}
.auth-card .auth-sub{color:#9aa0b4;margin:0 0 24px 0;font-size:13px}
.field{margin-bottom:14px}
.field label{display:block;font-size:12px;color:#9aa0b4;margin-bottom:6px;font-weight:500}
.field input,.field select{width:100%;padding:10px 12px;background:#0e1320;border:1px solid #252837;border-radius:7px;color:#e8e8f0;font-size:14px;font-family:inherit}
.field input:focus,.field select:focus{outline:none;border-color:#22c55e;background:#141822}
.tabs{display:flex;gap:8px;margin-bottom:20px;border-bottom:1px solid #1f2533}
.tab{padding:10px 16px;cursor:pointer;color:#9aa0b4;border-bottom:2px solid transparent;font-weight:500}
.tab.active{color:#22c55e;border-bottom-color:2#22c55e}
.error{background:rgba(239,68,68,0.15);color:#fca5a5;padding:10px 14px;border-radius:8px;border:1px solid rgba(239,68,68,0.3);font-size:13px;margin-bottom:14px}
.success{background:rgba(34,197,94,0.15);color:#86efac;padding:10px 14px;border-radius:8px;border:1px solid rgba(34,197,94,0.3);font-size:13px;margin-bottom:14px}
.info{background:rgba(96,165,250,0.12);color:#93c5fd;padding:10px 14px;border-radius:8px;border:1px solid rgba(96,165,250,0.3);font-size:13px;margin-bottom:14px}
.stat-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px;margin-bottom:24px}
.stat-card{background:rgba(20,24,34,0.7);border:1px solid #252837;border-radius:12px;padding:16px 18px}
.stat-card .label{font-size:11px;color:#9aa0b4;text-transform:uppercase;letter-spacing:0.06em;font-weight:500}
.stat-card .value{font-size:26px;font-weight:600;margin-top:6px;font-variant-numeric:tabular-nums}
.stat-card .sub{font-size:11px;color:#6b7280;margin-top:4px}
table{width:100%;border-collapse:collapse;background:rgba(20,24,34,0.7);border:1px solid #252837;border-radius:10px;overflow:hidden;font-size:13px}
th,td{padding:10px 14px;text-align:left;border-bottom:1px solid #1a1d29}
th{background:#1a1d29;color:#9aa0b4;font-weight:600;text-transform:uppercase;font-size:10px;letter-spacing:0.06em}
tr:hover{background:#1a1d2c}
td.mono{font-family:"SF Mono",Consolas,monospace;font-size:11px;color:#9aa0b4}
td .pill{display:inline-block;padding:2px 8px;border-radius:10px;font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:0.05em}
.pill-queued{background:rgba(245,158,11,0.18);color:#f59e0b}
.pill-running{background:rgba(59,130,246,0.18);color:#60a5fa}
.pill-paused{background:rgba(168,85,247,0.18);color:#a855f7}
.pill-succeeded{background:rgba(34,197,94,0.18);color:#22c55e}
.pill-failed{background:rgba(239,68,68,0.18);color:#ef4444}
.pill-invalid{background:rgba(168,85,247,0.18);color:#a855f7}
.action-link{color:#60a5fa;text-decoration:none}
.action-link:hover{text-decoration:underline}
.upload-zone{border:2px dashed #252837;border-radius:10px;padding:30px;text-align:center;cursor:pointer;transition:all 0.2s;background:#0e1320}
.upload-zone:hover,.upload-zone.dragover{border-color:#22c55e;background:#141822}
.upload-zone .hint{font-size:13px;color:#9aa0b4;margin-top:10px}
.job-progress{display:flex;align-items:center;gap:12px;margin:12px 0}
.job-progress .bar{flex:1;height:8px;background:#252837;border-radius:4px;overflow:hidden}
.job-progress .fill{display:block;height:100%;background:linear-gradient(90deg,#60a5fa,#22c55e);border-radius:4px;transition:width 0.4s}
.job-progress .pct{font-variant-numeric:tabular-nums;color:#9aa0b4;min-width:48px;text-align:right}
.node-pill{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;background:#252837;border-radius:10px;font-size:11px;font-weight:500}
.node-pill .dot{width:6px;height:6px;border-radius:50%;background:#22c55e}
.node-pill.busy .dot{background:#f59e0b;animation:pulse 1s infinite}
.node-pill.full .dot{background:#ef4444}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.3}}
.empty-state{padding:40px 20px;text-align:center;color:#6b7280;font-style:italic}
.muted{color:#9aa0b4}
.token-display{background:#0e1320;padding:10px 14px;border-radius:8px;font-family:"SF Mono",Consolas,monospace;font-size:12px;word-break:break-all;margin:12px 0;border:1px solid #252837;color:#fbbf24}
</style>
</head>
<body>
<div class="wrap" id="root">Loading…</div>
<script>
const API = ""; // same origin
let me = null;

async function api(method, url, body, isForm=false) {
  const opts = { method, headers: {} };
  if (body && !isForm) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  if (body && isForm) opts.body = body;
  const r = await fetch(API + url, opts);
  const text = await r.text();
  let data; try { data = JSON.parse(text); } catch { data = { ok:false, error: text }; }
  if (!r.ok) throw new Error(data.detail || data.error || r.statusText);
  return data;
}

async function load() {
  try { me = await api("GET", "/api/v1/auth/me"); } catch { me = null; }
  render();
}

function esc(s) { return String(s ?? "").replace(/[<>&"']/g, c => ({"<":"&lt;",">":"&gt;","&":"&amp;",'"':"&quot;","'":"&#39;"}[c])); }
function fmtSec(s) {
 if (s == null || s === 0) return "—";
  if (s < 60) return s.toFixed(1) + "s";
  if (s < 3600) return Math.floor(s/60) + "m " + Math.floor(s%60) + "s";
  return Math.floor(s/3600) + "h " + Math.floor((s%3600)/60) + "m";
}
function fmtAgo(epoch) {
  if (!epoch) return "—";
  const dt = Date.now()/1000 - epoch;
  if (dt < 60) return Math.floor(dt) + "s ago";
  if (dt < 3600) return Math.floor(dt/60) + "m ago";
  if (dt < 86400) return Math.floor(dt/3600) + "h ago";
  return Math.floor(dt/86400) + "d ago";
}

function headerBar() {
  const right = me ? `
    <div class="user-menu">
      <span class="muted">${esc(me.user.email || me.user.user_id)} · ${me.user.monthly_used}/${me.user.monthly_quota} jobs</span>
      <a href="/api/app/jobs">My Jobs</a><a href="/api/app/submit" style="background:linear-gradient(135deg,#22c55e,#10b981);color:#0a0c14;padding:6px 12px;border-radius:6px;text-decoration:none">+ Submit</a>
      <button onclick="logout()">Logout</button>
    </div>` : `
    <div class="user-menu">
      <a href="/api/app?tab=login">Sign in</a>
      <a class="btn btn-primary" href="/api/app?tab=signup">Get started</a>
    </div>`;
  return `<header><div class="brand">
    <div class="brand-mark">🎬</div><div><h1>V3 Studio</h1><p class="tagline">AI-powered green-screen video rendering</p></div>
  </div>${right}</header>`;
}

function render() {
  const root = document.getElementById("root");
  if (!me) {
    const params = new URLSearchParams(location.search);
    const tab = params.get("tab") || "signup";
    root.innerHTML = headerBar() + renderAuth(tab);
  } else {
    root.innerHTML = headerBar() + renderDashboard();
  }
}

function renderAuth(initialTab) {
  return `<div class="auth-card card">
    <h2 id="auth-title">${initialTab === "login" ? "Sign in" : "Create your account"}</h2>
    <p class="auth-sub">${initialTab === "login" ? "Access your renders" : "Start rendering green-screen videos — no credit card"}</p>
    <div id="auth-error"></div>
    <div id="auth-success"></div>
    <div class="tabs">
      <div class="tab ${initialTab === "signup" ? "active":""}" onclick="switchTab('signup')">Sign up</div>
      <div class="tab ${initialTab === "login" ? "active":""}" onclick="switchTab('login')">Sign in</div>
    </div>
    <form onsubmit="return handleAuth(event)">
      <div id="signup-fields" style="display:${initialTab === "signup" ? "block":"none"}">
        <div class="field"><label>Email</label><input type="email" name="email" required></div>
        <div class="field"><label>Display name (optional)</label><input type="text" name="display_name"></div>
        <div class="field"><label>Password (min 8 chars)</label><input type="password" name="password" required minlength="8"></div>
      </div>
      <div id="login-fields" style="display:${initialTab === "login" ? "block":"none"}">
        <div class="field"><label>Email</label><input type="email" name="email" required></div>
        <div class="field"><label>Password</label><input type="password" name="password" required></div>
      </div>
      <button class="btn btn-primary" type="submit" style="width:100%;justify-content:center">${initialTab === "login" ? "Sign in" : "Create account"}</button>
    </form>
  </div>`;
}

function switchTab(tab) {
  const root = document.getElementById("root");
  root.innerHTML = headerBar() + renderAuth(tab);
}

async function handleAuth(e) {
  e.preventDefault();
  const f = e.target;
  const err = document.getElementById("auth-error");
  const ok = document.getElementById("auth-success");
  err.innerHTML = ""; ok.innerHTML = "";
  const email = f.email.value.trim();
  const password = f.password.value;
  const display_name = f.display_name?.value || null;
  const params = new URLSearchParams(location.search);
  const tab = params.get("tab") || "signup";
  try {
    if (tab === "signup") {
      const data = await api("POST", "/api/v1/auth/signup", { email, password, display_name });
      ok.innerHTML = `<div class="success">✓ Account created! Save your API key (shown only once):<div class="token-display">${esc(data.api_key)}</div><button class="btn btn-secondary" onclick="navigator.clipboard.writeText('${data.api_key}');this.textContent='✓ Copied!'">Copy API key</button></div>`;
      setTimeout(async () => { me = await api("GET", "/api/v1/auth/me"); render(); }, 8000);
    } else {
      await api("POST", "/api/v1/auth/login", { email, password });
      me = await api("GET", "/api/v1/auth/me");
      location.href = "/api/app/jobs";
    }
  } catch (ex) { err.innerHTML = `<div class="error">✕ ${esc(ex.message)}</div>`; }
  return false;
}

async function logout() {
  await api("POST", "/api/v1/auth/logout", {});
  me = null;
  render();
}

async function renderDashboard() {
  const data = await api("GET", "/api/v1/users/me/jobs?limit=20");
  const jobs = data.jobs || [];
  const ok = jobs.filter(j => j.status === "succeeded" || j.status === "SUCCEEDED").length;
  const running = jobs.filter(j => j.status === "running").length;
  const total_sec = jobs.filter(j => j.finished_at && j.started_at).reduce((s, j) => s + (j.finished_at - j.started_at), 0);
  const html = `<div class="stat-grid">
    <div class="stat-card"><div class="label">Monthly Quota</div><div class="value">${me.user.monthly_used}/${me.user.monthly_quota}</div><div class="sub">${me.user.monthly_quota - me.user.monthly_used} remaining</div></div>
    <div class="stat-card"><div class="label">Active Jobs</div><div class="value">${running}</div><div class="sub">${jobs.length} total</div></div>
    <div class="stat-card"><div class="label">Success Rate</div><div class="value">${jobs.length ? Math.round(100 * ok / jobs.length) : 0}%</div><div class="sub">${ok}/${jobs.length} succeeded</div></div>
    <div class="stat-card"><div class="label">API Key</div><div class="value mono" style="font-size:14px;color:#fbbf24">${esc(me.user.api_key_prefix)}</div><div class="sub">Save this to use the API</div></div>
  </div>
  <div class="card"><h2 style="margin-top:0">Recent Jobs</h2>${jobsTable(jobs)}</div>`;
  document.getElementById("root").innerHTML = headerBar() + `<h1 style="font-size:18px;margin-bottom:16px">Welcome, ${esc(me.user.display_name || me.user.email)}</h1>` + html;
}

function jobsTable(jobs) {
  if (!jobs.length) return '<div class="empty-state">No jobs yet — submit your first render from the API or /api/app/jobs</div>';
  return `<table><thead><tr><th>Job ID</th><th>TC</th><th>Status</th><th>Worker</th><th>Created</th><th>Duration</th></tr></thead><tbody>
    ${jobs.map(j => `<tr>
      <td class="mono"><a class="action-link" href="/api/app/job/${esc(j.job_id)}">${esc(j.job_id?.slice(-12))}</a></td>
      <td><span class="pill pill-${esc(j.tc)}">${esc((j.tc||'').toUpperCase())}</span></td>
      <td><span class="pill pill-${esc(j.status)}">${esc(j.status)}</span></td>
      <td class="mono">${esc(j.worker_id || "—")}</td>
      <td class="mono">${fmtAgo(j.created_at)}</td>
      <td class="mono">${j.started_at && j.finished_at ? fmtSec(j.finished_at - j.started_at) : "—"}</td>
    </tr>`).join("")}
  </tbody></table>`;
}

load();
</script>
</body>
</html>
"""


# (7871 bytes)
_JOBS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>My Jobs · V3 Studio</title>
<style>
*,*::before,*::after{box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
background:linear-gradient(180deg,#0e1320 0%,#0a0c14 100%);color:#e8e8f0;margin:0;min-height:100vh;font-size:14px}
.wrap{max-width:1180px;margin:0 auto;padding:24px 20px 60px}
header{display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #1f2533;padding-bottom:18px;margin-bottom:28px}
.brand{display:flex;align-items:center;gap:12px}
.brand-mark{width:42px;height:42px;border-radius:11px;background:linear-gradient(135deg,#22c55e 0%,#10b981 60%,#06b6d4 100%);display:flex;align-items:center;justify-content:center;font-size:21px}
h1{margin:0;font-size:22px;font-weight:600}
.tagline{margin:3px 0 0;font-size:12px;color:#9aa0b4}
.user-menu{display:flex;align-items:center;gap:14px;font-size:13px}
.user-menu a,.user-menu button{color:#60a5fa;text-decoration:none;background:none;border:none;cursor:pointer;font-family:inherit;font-size:13px;padding:6px 12px;border-radius:6px}
.user-menu a:hover,.user-menu button:hover{background:#252837;color:#e8e8f0}
.card{background:rgba(20,24,34,0.7);border:1px solid #252837;border-radius:14px;padding:22px;margin-bottom:20px}
.btn{display:inline-flex;align-items:center;gap:8px;padding:10px 18px;border-radius:8px;border:none;cursor:pointer;font-family:inherit;font-weight:500;font-size:14px;text-decoration:none}
.btn-primary{background:linear-gradient(135deg,#22c55e,#10b981);color:#0a0c14}
.btn-primary:hover{transform:translateY(-1px);box-shadow:0 6px 18px rgba(34,197,94,0.25)}
.btn-secondary{background:#252837;color:#e8e8f0}
table{width:100%;border-collapse:collapse;background:rgba(20,24,34,0.7);border:1px solid #252837;border-radius:10px;overflow:hidden;font-size:13px}
th,td{padding:10px 14px;text-align:left;border-bottom:1px solid #1a1d29}
th{background:#1a1d29;color:#9aa0b4;font-weight:600;text-transform:uppercase;font-size:10px;letter-spacing:0.06em}
tr:hover{background:#1a1d2c}
td.mono{font-family:"SF Mono",Consolas,monospace;font-size:11px;color:#9aa0b4}
td .pill{display:inline-block;padding:2px 8px;border-radius:10px;font-size:10px;font-weight:600;text-transform:uppercase}
.pill-queued{background:rgba(245,158,11,0.18);color:#f59e0b}
.pill-running{background:rgba(59,130,246,0.18);color:#60a5fa}
.pill-succeeded{background:rgba(34,197,94,0.18);color:#22c55e}
.pill-failed{background:rgba(239,68,68,0.18);color:#ef4444}
.pill-invalid{background:rgba(168,85,247,0.18);color:#a855f7}
.action-link{color:#60a5fa;text-decoration:none}
.action-link:hover{text-decoration:underline}
.muted{color:#9aa0b4}
.empty-state{padding:40px 20px;text-align:center;color:#6b7280;font-style:italic}
.filter-row{display:flex;gap:10px;margin-bottom:16px;align-items:center;flex-wrap:wrap}
.filter-row select,.filter-row input{padding:8px 12px;background:#0e1320;border:1px solid #252837;border-radius:7px;color:#e8e8f0;font-family:inherit;font-size:13px}
</style>
</head>
<body>
<div class="wrap" id="root">Loading…</div>
<script>
async function api(method, url) {
  const r = await fetch(url, { method, credentials: "same-origin" });
  const text = await r.text();
  let d; try { d = JSON.parse(text); } catch { d = { ok:false, error: text }; }
  if (!r.ok) { location.href = "/api/app"; throw new Error("auth"); }
  return d;
}
function esc(s) { return String(s ?? "").replace(/[<>&"']/g, c => ({"<":"&lt;",">":"&gt;","&":"&gt;".replace,"&":"&amp;",'"':"&quot;","'":"&#39;"}[c])); }
function fmtSec(s) { if (s == null || s === 0) return "—"; if (s < 60) return s.toFixed(1) + "s"; if (s < 3600) return Math.floor(s/60) + "m"; return Math.floor(s/3600) + "h"; }
function fmtAgo(epoch) { if (!epoch) return "—"; const dt = Date.now()/1000 - epoch; if (dt < 60) return Math.floor(dt) + "s ago"; if (dt < 3600) return Math.floor(dt/60) + "m ago"; if (dt < 86400) return Math.floor(dt/3600) + "h ago"; return Math.floor(dt/86400) + "d ago"; }

async function load() {
  let me, jobsData;
  try { me = await api("GET", "/api/v1/auth/me"); } catch { location.href = "/api/app"; return; }
  jobsData = await api("GET", "/api/v1/users/me/jobs?limit=100");
  const jobs = jobsData.jobs || [];
  document.getElementById("root").innerHTML = `
    <header>
      <a href="/api/app" class="brand" style="text-decoration:none;color:inherit">
        <div class="brand-mark">🎬</div><div><h1>V3 Studio</h1><p class="tagline">${esc(me.user.email)}</p></div>
      </a>
      <div class="user-menu">
        <a href="/api/app">Home</a>
        <a href="/api/app/jobs">Jobs</a><a href="/api/app/submit" style="background:linear-gradient(135deg,#22c55e,#10b981);color:#0a0c14">+ Submit Job</a>
        <button onclick="logout()">Logout</button>
      </div>
    </header>
    <h1 style="font-size:18px;margin-bottom:16px">My Jobs (${jobs.length})</h1>
    <div class="card">
      <div class="filter-row">
        <select id="statusFilter"><option value="">All statuses</option><option value="running">Running</option><option value="queued">Queued</option><option value="succeeded">Succeeded</option><option value="failed">Failed</option></select>
        <select id="tcFilter"><option value="">All pipelines</option><option value="tc01">TC01</option><option value="tc02">TC02</option><option value="tc03">TC03</option><option value="tc04">TC04</option><option value="tc05">TC05</option><option value="tc06">TC06</option></select>
        <span class="muted">${jobs.length} jobs total</span>
      </div>
      ${table(jobs)}
    </div>`;
  document.getElementById("statusFilter").onchange = () => filter(jobs);
  document.getElementById("tcFilter").onchange = () => filter(jobs);
}

function filter(jobs) {
  const s = document.getElementById("statusFilter").value;
  const t = document.getElementById("tcFilter").value;
  const filtered = jobs.filter(j =>
    (!s || j.status === s || j.status === s.toUpperCase()) &&
    (!t || j.tc === t)
  );
  document.querySelector("#jobs-tbody").innerHTML = filtered.map(j =>
    `<tr><td class="mono"><a class="action-link" href="/api/app/job/${esc(j.job_id)}">${esc(j.job_id?.slice(-12))}</a></td><td><span class="pill pill-${esc(j.tc)}">${esc((j.tc||'').toUpperCase())}</span></td><td><span class="pill pill-${esc(j.status)}">${esc(j.status)}</span></td><td class="mono">${esc(j.worker_id || "—")}</td><td class="mono">${fmtAgo(j.created_at)}</td><td class="mono">${j.started_at && j.finished_at ? fmtSec(j.finished_at - j.started_at) : "—"}</td><td class="mono">${j.output_size ? (j.output_size/1024/1024).toFixed(1) + "MB" : "—"}</td></tr>`
  ).join("");
}

function table(jobs) {
  if (!jobs.length) return '<div class="empty-state">No jobs yet.</div>';
  return `<table><thead><tr><th>Job ID</th><th>TC</th><th>Status</th><th>Worker</th><th>Created</th><th>Duration</th><th>Output</th></tr></thead><tbody id="jobs-tbody">${
    jobs.map(j => `<tr><td class="mono"><a class="action-link" href="/api/app/job/${esc(j.job_id)}">${esc(j.job_id?.slice(-12))}</a></td><td><span class="pill pill-${esc(j.tc)}">${esc((j.tc||'').toUpperCase())}</span></td><td><span class="pill pill-${esc(j.status)}">${esc(j.status)}</span></td><td class="mono">${esc(j.worker_id || "—")}</td><td class="mono">${fmtAgo(j.created_at)}</td><td class="mono">${j.started_at && j.finished_at ? fmtSec(j.finished_at - j.started_at) : "—"}</td><td class="mono">${j.output_size ? (j.output_size/1024/1024).toFixed(1) + "MB" : "—"}</td></tr>`).join("")
  }</tbody></table>`;
}

async function logout() { await fetch("/api/v1/auth/logout", { method: "POST", credentials: "same-origin" }); location.href = "/api/app"; }

load();
setInterval(load, 30000);  // refresh every 30s
</script>
</body>
</html>
"""


# (10301 bytes)
_JOB_DETAIL_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Job · V3 Studio</title>
<style>
*,*::before,*::after{box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
background:linear-gradient(180deg,#0e1320 0%,#0a0c14 100%);color:#e8e8f0;margin:0;min-height:100vh;font-size:14px}
.wrap{max-width:980px;margin:0 auto;padding:24px 20px 60px}
header{display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #1f2533;padding-bottom:18px;margin-bottom:28px}
.brand{display:flex;align-items:center;gap:12px}
.brand-mark{width:42px;height:42px;border-radius:11px;background:linear-gradient(135deg,#22c55e 0%,#10b981 60%,#06b6d4 100%);display:flex;align-items:center;justify-content:center;font-size:21px}
h1{margin:0;font-size:18px;font-weight:600}
.tagline{margin:3px 0 0;font-size:11px;color:#9aa0b4;font-family:"SF Mono",Consolas,monospace}
.user-menu{display:flex;align-items:center;gap:14px;font-size:13px}
.user-menu a{color:#60a5fa;text-decoration:none}
.btn{display:inline-flex;align-items:center;gap:6px;padding:8px 14px;border-radius:8px;border:none;cursor:pointer;font-family:inherit;font-weight:500;font-size:13px;text-decoration:none}
.btn-secondary{background:#252837;color:#e8e8f0}
.card{background:rgba(20,24,34,0.7);border:1px solid #252837;border-radius:14px;padding:22px;margin-bottom:20px}
.card h2{margin:0 0 12px 0;font-size:13px;color:#9aa0b4;text-transform:uppercase;letter-spacing:0.06em;font-weight:600}
.hero-progress{text-align:center;padding:20px 0}
.hero-progress .pct{font-size:64px;font-weight:600;font-variant-numeric:tabular-nums;background:linear-gradient(135deg,#22c55e,#06b6d4);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.hero-progress .label{font-size:14px;color:#9aa0b4;margin-top:4px}
.progress-bar{height:14px;background:#252837;border-radius:7px;overflow:hidden;margin:14px 0}
.progress-bar .fill{height:100%;background:linear-gradient(90deg,#60a5fa,#22c55e);border-radius:7px;transition:width 0.5s}
.grid-2{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:14px}
@media(max-width:720px){.grid-2{grid-template-columns:1fr}}
.kv{display:grid;grid-template-columns:auto 1fr;gap:6px 14px;font-size:13px}
.kv dt{color:#9aa0b4}
.kv dd{margin:0;color:#e8e8f0}
.node-card{background:#0e1320;border:1px solid #252837;border-radius:10px;padding:14px;display:flex;align-items:center;gap:14px}
.node-icon{width:42px;height:42px;border-radius:10px;background:linear-gradient(135deg,#10b981,#22d3ee);display:flex;align-items:center;justify-content:center;font-size:20px}
.node-card .info{flex:1;min-width:0}
.node-card .name{font-weight:600;font-size:14px}
.node-card .tier{font-size:11px;color:#9aa0b4;text-transform:uppercase;margin-top:2px}
.node-card .load{font-size:11px;color:#9aa0b4;margin-top:2px}
.loadbar{height:6px;background:#252837;border-radius:3px;margin-top:6px;overflow:hidden}
.loadbar .fill{height:100%;background:linear-gradient(90deg,#60a5fa,#22c55e);border-radius:3px;transition:width 0.4s}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#22c55e;margin-right:6px;vertical-align:middle;animation:pulse 1.5s ease-in-out infinite}
.dot.busy{background:#f59e0b}
.dot.err{background:#ef4444}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.4}}
.log-box{background:#0e1320;border:1px solid #252837;border-radius:8px;padding:14px;font-family:"SF Mono",Consolas,monospace;font-size:11px;color:#9aa0b4;max-height:240px;overflow-y:auto;line-height:1.6;white-space:pre-wrap}
.output-list{display:grid;gap:6px}
.output-item{background:#0e1320;border:1px solid #252837;border-radius:8px;padding:8px 12px;font-family:"SF Mono",Consolas,monospace;font-size:12px;display:flex;justify-content:space-between;align-items:center}
.output-item a{color:#60a5fa;text-decoration:none}
.output-item a:hover{text-decoration:underline}
.muted{color:#9aa0b4}
</style>
</head>
<body>
<div class="wrap" id="root">Loading…</div>
<script>
let jobId = location.pathname.split("/").pop();

async function api(method, url) {
  const r = await fetch(url, { method, credentials: "same-origin" });
  const text = await r.text();
  let d; try { d = JSON.parse(text); } catch { d = { ok:false, error: text }; }
  if (!r.ok) throw new Error(d.detail || d.error || r.statusText);
  return d;
}
function esc(s) { return String(s ?? "").replace(/[<>&"']/g, c => ({"<":"&lt;",">":"&gt;","&":"&amp;",'"':"&quot;","'":"&#39;"}[c])); }
function fmtSec(s) { if (s == null || s === 0) return "—"; if (s < 60) return s.toFixed(1) + "s"; if (s < 3600) return Math.floor(s/60) + "m " + Math.floor(s%60) + "s"; return Math.floor(s/3600) + "h " + Math.floor((s%3600)/60) + "m"; }
function fmtAgo(epoch) { if (!epoch) return "—"; const dt = Date.now()/1000 - epoch; if (dt < 60) return Math.floor(dt) + "s ago"; if (dt < 3600) return Math.floor(dt/60) + "m ago"; if (dt < 86400) return Math.floor(dt/3600) + "h ago"; return Math.floor(dt/86400) + "d ago"; }
function fmtSize(b) { if (!b) return "—"; const u = ["B","KB","MB","GB"]; let i = 0; let v = b; while (v >= 1024 && i < u.length-1) { v/=1024; i++; } return v.toFixed(1) + " " + u[i]; }

async function load() {
  let job, me;
  try { me = await api("GET", "/api/v1/auth/me"); }
  catch { location.href = "/api/app"; return; }
  try { job = await api("GET", `/api/v1/jobs/${encodeURIComponent(jobId)}/live`); }
  catch (e) { document.getElementById("root").innerHTML = `<header><a href="/api/app" class="brand"><div class="brand-mark">🎬</div><div><h1>V3 Studio</h1><p class="tagline">Job not found</p></div></a></header><div class="card"><h2>Job not found</h2><p>This job may have been deleted or you don't have access to it.</p><a class="btn btn-secondary" href="/api/app/jobs">Back to my jobs</a></div>`; return; }

  const progress = Math.round((job.progress || 0) * 100);
  const status = (job.status || "unknown").toLowerCase();
  const isActive = ["running","queued","paused"].includes(status);
  const dotClass = status === "running" ? "busy" : (status === "failed" ? "err" : "");

  const workerBlock = job.worker ? `
    <div class="card">
      <h2>Assigned Worker</h2>
      <div class="node-card">
        <div class="node-icon">🖥️</div>
        <div class="info">
          <div class="name">${esc(job.worker.node)} <span style="color:#9aa0b4;font-weight:400;font-size:12px">(${esc(job.worker.tier)})</span></div>
          <div class="load">Load: ${esc(job.worker_load?.active_jobs ?? "—")} / ${esc(job.worker.max_concurrent)} active jobs</div>
          <div class="loadbar"><div class="fill" style="width:${(job.worker_load?.active_jobs / job.worker.max_concurrent * 100) || 0}%"></div></div>
        </div>
      </div>
    </div>` : "";

  const eta = job.eta_seconds != null ? (status === "running" ? "≈ " + fmtSec(job.eta_seconds) + " remaining" : status === "queued" ? "≈ " + fmtSec(job.eta_seconds) + " wait + render" : "—") : "—";

  const logBox = job.log && job.log.length ? `<div class="log-box">${esc(job.log.slice(-30).map(l => typeof l === 'string' ? l : JSON.stringify(l)).join("\\n"))}</div>` : '<div class="log-box">No log output yet.</div>';

  const outputList = job.output_files && job.output_files.length ? `
    <div class="output-list">${job.output_files.map(f => { const fn = typeof f === 'string' ? f.split('/').pop() : (f.name || JSON.stringify(f)); return `<div class="output-item"><span>${esc(fn)}</span><a href="/api/v1/jobs/${encodeURIComponent(job.job_id)}/download/${encodeURIComponent(fn)}" target="_blank">Download →</a></div>`; }).join("")}</div>` : '<div class="muted">No outputs yet.</div>';

  document.getElementById("root").innerHTML = `
    <header>
      <a href="/api/app/jobs" class="brand" style="text-decoration:none;color:inherit">
        <div class="brand-mark">🎬</div><div><h1>Job ${esc(job.job_id?.slice(-12))}</h1><p class="tagline">${esc(jobId)}</p></div>
      </a>
      <div class="user-menu"><a href="/api/app/jobs">Back to jobs</a><a href="/api/app/submit" style="background:linear-gradient(135deg,#22c55e,#10b981);color:#0a0c14;padding:6px 12px;border-radius:6px;text-decoration:none;margin-left:8px">+ New Job</a></div>
    </header>

    <div class="card">
      <div class="hero-progress">
        <div class="pct">${progress}%</div>
        <div class="label"><span class="dot ${dotClass}"></span>${esc(status.toUpperCase())} ${eta ? "· " + eta : ""}</div>
      </div>
      <div class="progress-bar"><div class="fill" style="width:${progress}%"></div></div>
      <div class="grid-2">
        <dl class="kv">
          <dt>Pipeline</dt><dd>${esc((job.tc||'').toUpperCase())}</dd>
          <dt>Status</dt><dd><span class="muted">${esc(status)}</span></dd>
          <dt>Step</dt><dd>${esc(job.current_step || "—")}</dd>
          <dt>Created</dt><dd>${fmtAgo(job.created_at)}</dd>
          <dt>Started</dt><dd>${job.started_at ? fmtAgo(job.started_at) : "—"}</dd>
          <dt>Duration</dt><dd>${job.started_at && job.finished_at ? fmtSec(job.finished_at - job.started_at) : isActive ? fmtSec(Date.now()/1000 - job.started_at) + " (running)" : "—"}</dd>
        </dl>
        <dl class="kv">
          <dt>Output</dt><dd>${job.output_file ? esc(job.output_file.split('/').pop()) : "—"}</dd>
          <dt>Size</dt><dd>${fmtSize(job.output_size)}</dd>
          <dt>Worker</dt><dd>${job.worker ? esc(job.worker.node) : "—"}${job.worker_load ? " (" + job.worker_load.active_jobs + "/" + job.worker.max_concurrent + " active)" : ""}</dd>
          <dt>Avg for ${esc((job.tc||'').toUpperCase())}</dt><dd>≈ ${fmtSec(job.avg_seconds)}</dd>
        </dl>
      </div>
      ${job.error ? `<div style="margin-top:14px;background:rgba(239,68,68,0.15);color:#fca5a5;padding:12px 16px;border-radius:8px;font-size:13px;font-family:'SF Mono',Consolas,monospace">${esc(job.error)}</div>` : ""}
    </div>

    ${workerBlock}

    <div class="card">
      <h2>Output Files (${job.output_files?.length || 0})</h2>
      ${outputList}
    </div>

    <div class="card">
      <h2>Render Log (last 30 lines)</h2>
      ${logBox}
    </div>
  `;
}

load();
if (location.search.includes("live=1")) setInterval(load, 2000);
</script>
</body>
</html>
"""


# (21267 bytes)
_PUBLIC_DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>V3 Cluster Status</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background:#0a0c14; color:#e8e8f0; margin:0; padding:20px; font-size:14px; }
  h1 { margin:0; font-size:22px; font-weight:600; }
  h2 { margin:28px 0 12px 0; font-size:13px; color:#9aa0b4; text-transform:uppercase; letter-spacing:0.08em; font-weight:600; }
  h2 .badge { float:inline-end; font-size:11px; padding:2px 8px; background:#252837; border-radius:4px; text-transform:none; letter-spacing:0; color:#9aa0b4; font-weight:500; cursor:pointer; border:none; font-family:inherit; }
  h2 .badge:hover { background:#3a3f55; color:#e8e8f0; }
  .header { display:flex; justify-content:space-between; align-items:baseline; margin-bottom:4px; }
  .subheader { color:#9aa0b4; font-size:12px; margin-bottom:20px; }
  .last-update { color:#6b7280; font-size:11px; }
  .grid { display:grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap:12px; }
  .grid-4 { display:grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap:10px; }
  .grid-2 { display:grid; grid-template-columns: 1fr 1fr 1fr; gap:12px; }
  .card { background:#141822; border:1px solid #252837; border-radius:8px; padding:14px 16px; position:relative; overflow:hidden; }
  .card.healthy { border-color:rgba(34,197,94,0.4); }
  .card.unhealthy { border-color:rgba(239,68,68,0.4); }
  .card.warning { border-color:rgba(245,158,11,0.4); }
  .card .label { font-size:11px; color:#9aa0b4; text-transform:uppercase; letter-spacing:0.06em; font-weight:500; }
  .card .value { font-size:28px; font-weight:600; margin-top:6px; font-variant-numeric:tabular-nums; }
  .card .sub { font-size:11px; color:#6b7280; margin-top:2px; }
  .card .value .ok { color:#22c55e; }
  .card .value .warn { color:#f59e0b; }
  .card .value .err { color:#ef4444; }
  .workers { display:grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap:14px; }
  .worker { background:#141822; border:1px solid #252837; border-radius:10px; padding:16px 18px; transition: border-color 0.3s, box-shadow 0.3s; }
  .worker.healthy { border-color:rgba(34,197,94,0.3); }
  .worker.unhealthy { border-color:rgba(239,68,68,0.4); box-shadow: 0 0 0 1px rgba(239,68,68,0.2); }
  .worker.disabled { opacity:0.5; border-color:rgba(107,114,128,0.3); }
  .worker-header { display:flex; justify-content:space-between; align-items:flex-start; margin-bottom:10px; }
  .worker-name { font-weight:600; font-size:14px; line-height:1.3; }
  .worker-id { font-family: "SF Mono", Consolas, monospace; font-size:11px; color:#9aa0b4; margin-top:1px; }
  .worker-tier { font-size:10px; padding:1px 6px; border-radius:3px; margin-left:6px; vertical-align:middle; }
  .tier-low { background:#3a3f55; color:#9aa0b4; }
  .tier-mid { background:rgba(245,158,11,0.2); color:#f59e0b; }
  .tier-high { background:rgba(168,85,247,0.2); color:#a855f7; }
  .status-pill { display:inline-block; padding:2px 8px; border-radius:10px; font-size:10px; font-weight:600; text-transform:uppercase; letter-spacing:0.05em; }
  .status-healthy { background:rgba(34,197,94,0.2); color:#22c55e; }
  .status-unhealthy { background:rgba(239,68,68,0.2); color:#ef4444; }
  .status-disabled { background:rgba(107,114,128,0.2); color:#9aa0b4; }
  .status-busy { background:rgba(245,158,11,0.2); color:#f59e0b; }
  .worker-meta { display:grid; grid-template-columns: auto 1fr; gap:4px 12px; font-size:12px; margin-top:8px; }
  .worker-meta dt { color:#9aa0b4; }
  .worker-meta dd { color:#e8e8f0; margin:0; font-family:"SF Mono",Consolas,monospace; font-size:11px; }
  .bar { display:block; height:6px; background:#252837; border-radius:3px; overflow:hidden; margin-top:8px; }
  .bar > * { display:block; height:100%; background:linear-gradient(90deg,#22c55e,#10b981); transition: width 0.5s; }
  .util { display:flex; justify-content:space-between; font-size:11px; color:#9aa0b4; margin-bottom:4px; }
  .jobs-feed { background:#141822; border:1px solid #252837; border-radius:8px; padding:4px; max-height:400px; overflow-y:auto; }
  .job-row { display:grid; grid-template-columns: 80px 60px 1fr auto auto; gap:12px; padding:10px 12px; border-bottom:1px solid #252837; align-items:center; font-size:12px; }
  .job-row:last-child { border-bottom: none; }
  .job-row .status { padding:2px 8px; border-radius:10px; font-size:10px; font-weight:600; text-transform:uppercase; }
  .job-row .s-running { background:rgba(59,130,246,0.2); color:#60a5fa; }
  .job-row .s-queued { background:rgba(245,158,11,0.2); color:#f59e0b; }
  .job-row .s-paused { background:rgba(168,85,247,0.2); color:#a855f7; }
  .job-row .s-succeeded { background:rgba(34,197,94,0.2); color:#22c55e; }
  .job-row .s-failed { background:rgba(239,68,68,0.2); color:#ef4444; }
  .job-row .job-id { font-family:"SF Mono",Consolas,monospace; color:#9aa0b4; font-size:11px; }
  .job-row .progress { display:flex; align-items:center; gap:8px; }
  .job-row .progress-bar { width:120px; height:5px; background:#252837; border-radius:2px; overflow:hidden; }
  .job-row .progress-bar > * { display:block; height:100%; background:linear-gradient(90deg,#60a5fa,#22c55e); }
  .job-row .progress-pct { color:#9aa0b4; font-variant-numeric:tabular-nums; min-width:42px; }
  .job-row .meta { color:#6b7280; font-size:11px; }
  .job-row .tc-pill { padding:2px 6px; border-radius:3px; background:#3a3f55; font-size:10px; font-weight:600; }
  table { width:100%; border-collapse:collapse; background:#141822; border:1px solid #252837; border-radius:8px; overflow:hidden; font-size:12px; }
  th, td { padding:8px 12px; text-align:left; border-bottom:1px solid #1a1d29; }
  th { background:#1a1d29; color:#9aa0b4; font-weight:600; text-transform:uppercase; font-size:10px; letter-spacing:0.06em; }
  tr:hover { background:#1a1d2c; }
  td.mono { font-family:"SF Mono",Consolas,monospace; font-size:11px; color:#9aa0b4; }
  td.right { text-align:right; font-variant-numeric:tabular-nums; }
  td .pill { display:inline-block; padding:1px 6px; border-radius:3px; font-size:10px; font-weight:600; }
  td .pill.ok { background:rgba(34,197,94,0.2); color:#22c55e; }
  td .pill.fail { background:rgba(239,68,68,0.2); color:#ef4444; }
  td .pill.invalid { background:rgba(168,85,247,0.2); color:#a855f7; }
  td .pill.queued { background:rgba(245,158,11,0.2); color:#f59e0b; }
  .chart-box { background:#141822; border:1px solid #252837; border-radius:8px; padding:16px; height:200px; position:relative; }
  .chart-box h3 { margin:0 0 10px 0; font-size:11px; color:#9aa0b4; text-transform:uppercase; letter-spacing:0.06em; font-weight:600; }
  .chart-canvas-wrap { position:relative; height:calc(100% - 22px); }
  .empty { color:#6b7280; font-style:italic; padding:24px; text-align:center; }
  .spinner { display:inline-block; width:14px; height:14px; border:2px solid #252837; border-top-color:#60a5fa; border-radius:50%; animation:spin 0.8s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .footer { color:#6b7280; font-size:11px; margin-top:32px; text-align:center; padding:16px; }
  .pulse-dot { display:inline-block; width:6px; height:6px; background:#22c55e; border-radius:50%; margin-right:6px; animation:pulse 1.5s ease-in-out infinite; }
  @keyframes pulse { 0%,100% { opacity:1; } 50% { opacity:0.3; } }
  .metric-bar { display:flex; align-items:center; gap:8px; padding:6px 0; font-size:12px; }
  .metric-bar .name { width:80px; color:#9aa0b4; }
  .metric-bar .bar-track { flex:1; height:18px; background:#252837; border-radius:3px; position:relative; overflow:hidden; }
  .metric-bar .bar-fill { position:absolute; left:0; top:0; height:100%; background:linear-gradient(90deg,#60a5fa,#22c55e); display:flex; align-items:center; padding-left:8px; font-size:10px; font-weight:600; color:#0a0c14; }
  .metric-bar .bar-val { width:90px; text-align:right; font-variant-numeric:tabular-nums; color:#e8e8f0; }
  @media (max-width: 900px) { .grid-2 { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<div class="header">
  <div>
    <h1>🟢 V3 Cluster Status <span class="pulse-dot"></span></h1>
    <div class="subheader"><span id="clock"></span> · <span class="last-update" id="lastUpdate">—</span></div>
  </div>
  <div>
    <select id="intervalSel" class="badge" onchange="setInterval(load, parseInt(this.value))">
      <option value="5000">↻ 5s</option>
      <option value="10000" selected>↻ 10s</option>
      <option value="30000">↻ 30s</option>
      <option value="60000">↻ 60s</option>
    </select>
  </div>
</div>

<h2>Cluster Summary</h2>
<div class="grid-4">
  <div class="card" id="cardWorkers"><div class="label">Workers</div><div class="value" id="vWorkers">—</div><div class="sub" id="sWorkers">—</div></div>
  <div class="card" id="cardHealthy"><div class="label">Healthy</div><div class="value ok" id="vHealthy">—</div><div class="sub" id="sHealthy">—</div></div>
  <div class="card" id="cardActive"><div class="label">Active Jobs</div><div class="value" id="vActive">—</div><div class="sub" id="sActive">—</div></div>
  <div class="label card" id="cardSuccess"><div class="label">Success Rate (24h)</div><div class="value" id="vSuccess">—</div><div class="sub" id="sSuccess">—</div></div>
</div>

<h2>Workers <button class="badge" onclick="testAllWorkers()">🔌 test all</button></h2>
<div class="workers" id="workersGrid"><div class="empty">Loading workers…</div></div>

<h2>Live Jobs</h2>
<div class="jobs-feed" id="liveJobs"><div class="empty">Loading jobs…</div></div>

<h2>Performance (last 24h)</h2>
<div class="grid-2">
  <div class="chart-box"><h3>Throughput · jobs/hour</h3><div class="chart-canvas-wrap"><canvas id="chartThroughput"></canvas></div></div>
  <div class="chart-box"><h3>Latency p50 + p95 by TC</h3><div class="chart-canvas-wrap"><canvas id="chartLatency"></canvas></div></div>
  <div class="chart-box"><h3>Job volume by TC</h3><div class="chart-canvas-wrap"><canvas id="chartByTC"></canvas></div></div>
</div>

<h2>Per-Worker Stats (last 24h)</h2>
<div id="workerStats"><div class="empty">Loading…</div></div>

<script>
const INTERNAL = '__INT__';
const COLORS = {
  ok: '#22c55e', fail: '#ef4444', invalid: '#a855f7', queued: '#f59e0b',
  tc: { tc01:'#60a5fa', tc02:'#22c55e', tc03:'#f59e0b', tc04:'#a855f7', tc05:'#ec4899', tc06:'#14b8a6' },
};

let charts = {};
function esc(s) { return String(s ?? '').replace(/[<>&"']/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;',"'":'&#39;'}[c])); }
function fmtBytes(n) {
  if (!n) return '—';
  const units = ['B','KB','MB','GB']; let i=0; let v=n;
  while (v >= 1024 && i < units.length-1) { v/=1024; i++; }
  return v.toFixed(1) + ' ' + units[i];
}
function fmtSec(s) {
  if (s == null) return '—';
  if (s < 60) return s.toFixed(1) + 's';
  if (s < 3600) return Math.floor(s/60) + 'm ' + (s%60).toFixed(0) + 's';
  return Math.floor(s/3600) + 'h ' + Math.floor((s%3600)/60) + 'm';
}
function fmtTimeAgo(epoch) {
  if (!epoch) return '—';
  const dt = Date.now()/1000 - epoch;
  if (dt < 60) return Math.floor(dt) + 's ago';
  if (dt < 3600) return Math.floor(dt/60) + 'm ago';
  if (dt < 86400) return Math.floor(dt/3600) + 'h ago';
  return Math.floor(dt/86400) + 'd ago';
}
function fmtClock(epoch) { return new Date(epoch * 1000).toLocaleTimeString(); }

async function load() {
  document.getElementById('clock').textContent = new Date().toLocaleString();
  let data;
  try {
    const r = await fetch('/api/cluster/dashboard', { headers: { 'X-Cutdee-Internal': INTERNAL } });
    data = await r.json();
  } catch (e) {
    document.getElementById('root').innerHTML = '<div class="empty">⚠ Failed to load: ' + esc(e.message) + '</div>';
    return;
  }
  if (!data.ok) { document.getElementById('liveJobs').innerHTML = '<div class="empty">API error</div>'; return; }
  document.getElementById('lastUpdate').textContent = 'Last fetch: ' + fmtClock(data.server_time);
  renderSummary(data);
  renderWorkers(data.cluster);
  renderLiveJobs(data.live_jobs);
  renderMetrics(data.metrics);
}

function renderSummary(d) {
  const s = d.summary;
  document.getElementById('vWorkers').innerHTML = s.total_workers + ' <span class="sub" style="font-size:14px; color:#6b7280;">total</span>';
  document.getElementById('sWorkers').textContent = `${s.enabled_workers} enabled · ${s.disabled_workers} disabled`;
  document.getElementById('vHealthy').textContent = s.healthy_workers + ' / ' + s.enabled_workers;
  document.getElementById('sHealthy').textContent = s.down_workers + ' down';
  document.getElementById('vActive').innerHTML = s.active_jobs + ' <span class="sub" style="font-size:14px; color:#6b7280;">/ ' + s.total_capacity + '</span>';
  document.getElementById('sActive').textContent = (s.total_capacity > 0 ? Math.round(s.active_jobs / s.total_capacity * 100) : 0) + '% capacity';
  const tot = d.metrics.totals;
  document.getElementById('vSuccess').innerHTML = tot.success_rate + '<span class="sub" style="font-size:14px;">%</span>';
  document.getElementById('sSuccess').textContent = `${tot.ok} ok / ${tot.fail} fail / ${tot.invalid} invalid`;
}

function renderWorkers(workers) {
  const html = workers.map(w => {
    let statusClass = 'unhealthy', statusText = '✕ DOWN';
    if (!w.enabled) { statusClass = 'disabled'; statusText = '○ DISABLED'; }
    else if (!w.healthy) { statusClass = 'unhealthy'; statusText = '✕ UNHEALTHY'; }
    else if (w.active_jobs > 0) { statusClass = 'busy'; statusText = '⟳ BUSY'; }
    else { statusClass = 'healthy'; statusText = '● IDLE'; }
    const sys = w.system || {};
    const gpu = w.gpu || {};
    const inflight = w.in_flight_jobs || [];
    const inflightHtml = inflight.length === 0
      ? '<div class="meta" style="color:#6b7280;">no in-flight jobs</div>'
      : inflight.map(j => `
        <div class="job-row" style="padding:6px 0; grid-template-columns: auto auto 1fr auto;">
          <code class="job-id">${esc(j.job_id?.slice(-16) || '?')}</code>
          <span class="tc-pill">${esc(j.tc?.toUpperCase() || '?')}</span>
          <span class="progress-bar"><span style="width:${Math.round((j.progress||0)*100)}%"></span></span>
          <span class="progress-pct">${Math.round((j.progress||0)*100)}%</span>
        </div>`).join('');
    const pct = w.max_concurrent > 0 ? (w.active_jobs / w.max_concurrent * 100) : 0;
    const gpuList = (gpu.available || []).slice(0, 3).map(g => `<span class="tc-pill" style="background:#252837;">${esc(g)}</span>`).join(' ');
    return `
      <div class="worker ${w.healthy ? 'healthy' : 'unhealthy'} ${!w.enabled ? 'disabled' : ''}">
        <div class="worker-header">
          <div>
            <div class="worker-name">${esc(w.name || w.id)}
              <span class="worker-tier tier-${esc(w.tier || 'low')}">${esc((w.tier || 'low').toUpperCase())}</span>
            </div>
            <div class="worker-id">${esc(w.id)}</div>
          </div>
          <div><span class="status-pill status-${statusClass}">${statusText}</span></div>
        </div>
        <div class="util">
          <span>${w.active_jobs} / ${w.max_concurrent} jobs</span>
          <span style="color:#6b7280;">${pct.toFixed(0)}% capacity</span>
        </div>
        <div class="bar"><span style="width:${pct}%"></span></div>
        <dl class="worker-meta">
          <dt>Encoder</dt><dd>${esc(w.encoder || '?')}</dd>
          <dt>Version</dt><dd>${esc(w.version || '—')} · ${esc(w.commit || '?')}</dd>
          <dt>GPU</dt><dd>${gpuList || '<span style="color:#6b7280;">none (CPU-only)</span>'}</dd>
          ${sys.disk_free_gb != null ? `<dt>Disk free</dt><dd>${sys.disk_free_gb.toFixed(1)} GB</dd>` : ''}
          ${sys.cpu_percent != null ? `<dt>CPU%</dt><dd>${sys.cpu_percent}%</dd>` : ''}
          <dt>Last seen</dt><dd>${fmtTimeAgo(w.last_seen)}</dd>
          ${w.error ? `<dt style="color:#ef4444;">Error</dt><dd style="color:#ef4444;">${esc(w.error)}</dd>` : ''}
        </dl>
        ${inflightHtml}
      </div>`;
  }).join('');
  document.getElementById('workersGrid').innerHTML = html || '<div class="empty">No workers configured</div>';
}

function renderLiveJobs(jobs) {
  if (!jobs || jobs.length === 0) {
    document.getElementById('liveJobs').innerHTML = '<div class="empty">No active jobs 🟢</div>';
    return;
  }
  const html = jobs.map(j => {
    const statusClass = 's-' + (j.status || 'unknown');
    const tcColor = COLORS.tc[j.tc?.toLowerCase()] || '#6b7280';
    const pct = Math.round((j.progress || 0) * 100);
    return `<div class="job-row">
      <span class="status ${statusClass}">${esc(j.status)}</span>
      <span class="tc-pill" style="background:${tcColor}; color:#0a0c14;">${esc((j.tc || '?').toUpperCase())}</span>
      <code class="job-id">${esc(j.job_id)}</code>
      <span class="progress">
        <div class="progress-bar"><span style="width:${pct}%"></span></div>
        <span class="progress-pct">${pct}%</span>
      </span>
      <span class="meta">${esc(j.worker_id || 'queued')} · ${fmtSec(j.elapsed_sec)}</span>
    </div>`;
  }).join('');
  document.getElementById('liveJobs').innerHTML = html;
}

function makeChart(id, type, data, options) {
  if (charts[id]) charts[id].destroy();
  const ctx = document.getElementById(id).getContext('2d');
  charts[id] = new Chart(ctx, { type, data, options });
}

const CHART_OPTS = {
  responsive: true, maintainAspectRatio: false,
  plugins: { legend: { labels: { color: '#9aa0b4', font: { size: 10 } } } },
  scales: {
    x: { ticks: { color: '#6b7280', font: { size: 10 } }, grid: { color: '#1a1d29' } },
    y: { ticks: { color: '#6b7280', font: { size: 10 } }, grid: { color: '#1a1d29' } },
  },
};

function renderMetrics(m) {
  // Throughput chart
  const hours = m.hourly_throughput.map(h => {
    const d = new Date(h.hour * 1000);
    return d.getHours().toString().padStart(2,'0') + ':00';
  });
  const totalSeries = m.hourly_throughput.map(h => h.total);
  const okSeries = m.hourly_throughput.map(h => h.ok);
  makeChart('chartThroughput', 'bar', {
    labels: hours,
    datasets: [
      { label: 'Total', data: totalSeries, backgroundColor: '#60a5fa88', borderColor: '#60a5fa', borderWidth: 1 },
      { label: 'OK', data: okSeries, backgroundColor: '#22c55e88', borderColor: '#22c55e', borderWidth: 1 },
    ],
  }, { ...CHART_OPTS, scales: { ...CHART_OPTS.scales, x: { ...CHART_OPTS.scales.x, ticks: { ...CHART_OPTS.scales.x.ticks, maxRotation: 0, autoSkip: true } } } });

  // Latency chart
  const tcs = m.by_tc.map(t => t.tc?.toUpperCase() || '?');
  const p50 = m.by_tc.map(t => t.p50_sec);
  const p95 = m.by_tc.map(t => t.p95_sec);
  makeChart('chartLatency', 'bar', {
    labels: tcs,
    datasets: [
      { label: 'p50', data: p50, backgroundColor: '#60a5fa', borderRadius: 4 },
      { label: 'p95', data: p95, backgroundColor: '#f59e0b', borderRadius: 4 },
    ],
  }, { ...CHART_OPTS, scales: { ...CHART_OPTS.scales, y: { ...CHART_OPTS.scales.y, ticks: { ...CHART_OPTS.scales.y.ticks, callback: v => v + 's' } } } });

  // By TC chart
  const tcOk = m.by_tc.map(t => t.ok);
  const tcFail = m.by_tc.map(t => t.fail);
  const tcInvalid = m.by_tc.map(t => t.invalid);
  makeChart('chartByTC', 'bar', {
    labels: tcs,
    datasets: [
      { label: 'OK', data: tcOk, backgroundColor: '#22c55e' },
      { label: 'Failed', data: tcFail, backgroundColor: '#ef4444' },
      { label: 'Invalid', data: tcInvalid, backgroundColor: '#a855f7' },
    ],
  }, { ...CHART_OPTS, scales: { ...CHART_OPTS.scales, x: { ...CHART_OPTS.scales.x, stacked: true }, y: { ...CHART_OPTS.scales.y, stacked: true } } });

  // Per-worker stats
  if (!m.by_worker || m.by_worker.length === 0) {
    document.getElementById('workerStats').innerHTML = '<div class="empty">No worker stats yet</div>';
    return;
  }
  const maxTotal = Math.max(...m.by_worker.map(w => w.total));
  document.getElementById('workerStats').innerHTML = m.by_worker.map(w => {
    const successPct = w.success_rate;
    const avgSec = w.avg_sec || 0;
    const totalBarWidth = (w.total / maxTotal * 100).toFixed(1);
    const okBarColor = successPct >= 90 ? '#22c55e' : successPct >= 70 ? '#f59e0b' : '#ef4444';
    return `<div class="metric-bar">
      <span class="name">${esc(w.worker_id.replace(/_/g, ' '))}</span>
      <div class="bar-track"><div class="bar-fill" style="width:${totalBarWidth}%; background:${okBarColor};">${w.total}</div></div>
      <span class="bar-val">${successPct}% ok · ${fmtSec(avgSec)}</span>
    </div>`;
  }).join('');
}

async function testAllWorkers() {
  if (!confirm('Test all worker connections? This calls /health on every worker.')) return;
  const INTL = INTERNAL;
  try {
    const r = await fetch('/api/cluster/workers/reload', { method: 'POST', headers: { 'X-Cutdee-Internal': INTL } });
    const d = await r.json();
    alert('Reloaded: ' + d.count + ' workers from disk. Dashboard will refresh next tick.');
    load();
  } catch (e) { alert('Error: ' + e.message); }
}

load();
setInterval(load, parseInt(document.getElementById('intervalSel').value));
</script>
</body>
</html>
"""


# (18377 bytes)
_ADMIN_DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>V3 Cluster · Status</title>
<meta name="description" content="Real-time public status for the V3 Cluster rendering platform.">
<meta name="robots" content="noindex, nofollow">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  *, *::before, *::after { box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
         background: linear-gradient(180deg, #0e1320 0%, #0a0c14 100%); color:#e8e8f0; margin:0; min-height:100vh; }
  .wrap { max-width: 1180px; margin: 0 auto; padding: 28px 24px 48px; }
  header { display:flex; justify-content:space-between; align-items:flex-end; border-bottom: 1px solid #1f2533; padding-bottom: 18px; margin-bottom: 28px; flex-wrap: wrap; gap: 12px; }
  .brand { display:flex; align-items:center; gap: 14px; }
  .brand-mark { width: 44px; height: 44px; border-radius: 12px;
                background: linear-gradient(135deg, #22c55e 0%, #10b981 60%, #06b6d4 100%);
                display:flex; align-items:center; justify-content:center; font-size:22px; box-shadow: 0 4px 14px rgba(34,197,94,0.25); }
  h1 { margin:0; font-size: 26px; font-weight: 600; letter-spacing: -0.01em; }
  .tagline { margin: 4px 0 0; font-size: 13px; color: #9aa0b4; }
  .updated { font-size: 11px; color: #6b7280; font-family: "SF Mono", Consolas, monospace; text-align: right; line-height:1.6; }
  .updated .live-dot { display:inline-block; width: 7px; height: 7px; background: #22c55e; border-radius: 50%;
                       margin-right: 6px; vertical-align: middle; animation: pulse 1.5s ease-in-out infinite; }
  @keyframes pulse { 0%,100% { opacity:1; box-shadow: 0 0 0 0 rgba(34,197,94,0.5); }
                       50% { opacity:0.4; box-shadow: 0 0 0 4px rgba(34,197,94,0); } }
  .stat-grid { display:grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 14px; margin-bottom: 32px; }
  .stat-card { background: rgba(20, 24, 34, 0.7); backdrop-filter: blur(8px); border: 1px solid #252837;
               border-radius: 14px; padding: 18px 20px; position:relative; overflow: hidden; transition: transform 0.2s, border-color 0.3s; }
  .stat-card:hover { transform: translateY(-1px); border-color: #2f3548; }
  .stat-card .label { font-size: 11px; color: #9aa0b4; text-transform: uppercase; letter-spacing: 0.08em; font-weight: 500; }
  .stat-card .value { font-size: 32px; font-weight: 600; margin-top: 8px; font-variant-numeric: tabular-nums; line-height: 1; }
  .stat-card .sub { font-size: 12px; color: #6b7280; margin-top: 6px; }
  .stat-card .accent { position:absolute; left: 0; top: 0; bottom: 0; width: 3px; }
  .accent-green { background: linear-gradient(180deg, #22c55e, #10b981); }
  .accent-blue { background: linear-gradient(180deg, #60a5fa, #22d3ee); }
  .accent-orange { background: linear-gradient(180deg, #f59e0b, #ef4444); }
  .accent-purple { background: linear-gradient(180deg, #a855f7, #ec4899); }
  .ok { color: #22c55e; }
  .warn { color: #f59e0b; }
  .err { color: #ef4444; }
  h2 { margin: 36px 0 14px; font-size: 13px; color: #9aa0b4; text-transform: uppercase; letter-spacing: 0.08em; font-weight: 600; }
  .nodes-grid { display:grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 14px; }
  .node { background: rgba(20, 24, 34, 0.7); border: 1px solid #252837; border-radius: 12px; padding: 16px 18px;
          transition: border-color 0.3s, opacity 0.3s; position: relative; }
  .node.online { border-color: rgba(34,197,94,0.35); }
  .node.offline { border-color: rgba(239,68,68,0.4); opacity: 0.85; }
  .node.disabled { border-color: rgba(107,114,128,0.3); opacity: 0.45; }
  .node-header { display:flex; justify-content:space-between; align-items:flex-start; margin-bottom: 10px; }
  .node-name { font-weight: 600; font-size: 14px; }
  .node-tier { font-size: 10px; padding: 2px 8px; border-radius: 4px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; margin-left: 6px; vertical-align: middle; }
  .tier-Standard { background: rgba(100,116,139,0.2); color: #94a3b8; }
  .tier-Performance { background: rgba(245,158,11,0.18); color: #f59e0b; }
  .tier-Compute+GPU { background: rgba(168,85,247,0.18); color: #a855f7; }
  .node-status { display:inline-block; padding: 2px 8px; border-radius: 10px; font-size: 10px; font-weight: 600; text-transform: uppercase; }
  .ns-online { background: rgba(34,197,94,0.2); color: #22c55e; }
  .ns-offline { background: rgba(239,68,68,0.2); color: #ef4444; }
  .ns-disabled { background: rgba(107,114,128,0.2); color: #9aa0b4; }
  .util { display:flex; justify-content:space-between; font-size: 11px; color: #9aa0b4; margin: 8px 0 4px; }
  .bar { height: 6px; background: #252837; border-radius: 3px; overflow: hidden; }
  .bar-fill { height: 100%; background: linear-gradient(90deg, #60a5fa, #22c55e); border-radius: 3px; transition: width 0.5s; }
  .bar-fill.busy { background: linear-gradient(90deg, #f59e0b, #ef4444); }
  .node-meta { display:grid; grid-template-columns: auto 1fr; gap: 4px 12px; font-size: 11px; margin-top: 12px; padding-top: 12px; border-top: 1px solid #1f2533; }
  .node-meta dt { color: #9aa0b4; }
  .node-meta dd { margin: 0; color: #e8e8f0; }
  .charts-grid { display:grid; grid-template-columns: 1fr 1fr; gap: 14px; }
  @media (max-width: 720px) { .charts-grid { grid-template-columns: 1fr; } }
  .chart-box { background: rgba(20, 24, 34, 0.7); border: 1px solid #252837; border-radius: 12px; padding: 18px; }
  .chart-box h3 { margin: 0 0 4px; font-size: 12px; color: #9aa0b4; text-transform: uppercase; letter-spacing: 0.06em; font-weight: 600; }
  .chart-box .sub-h { font-size: 11px; color: #6b7280; margin-bottom: 12px; }
  .chart-canvas-wrap { position: relative; height: 220px; }
  .tier-bar { display:flex; align-items: center; gap: 12px; padding: 8px 0; font-size: 12px; }
  .tier-bar .name { width: 110px; color: #9aa0b4; }
  .tier-bar .track { flex: 1; height: 22px; background: #252837; border-radius: 4px; position: relative; overflow: hidden; }
  .tier-bar .fill { position: absolute; left: 0; top: 0; height: 100%; display: flex; align-items: center; padding-left: 10px; font-size: 11px; font-weight: 600; color: #0a0c14; }
  .tier-bar .ok { background: linear-gradient(90deg, #60a5fa, #22c55e); }
  .tier-bar .warn { background: linear-gradient(90deg, #fbbf24, #f59e0b); }
  .tier-bar .err { background: linear-gradient(90deg, #f87171, #ef4444); }
  .tier-bar .val { width: 110px; text-align: right; font-variant-numeric: tabular-nums; color: #e8e8f0; }
  footer { text-align: center; margin-top: 56px; padding-top: 24px; border-top: 1px solid #1f2533; color: #6b7280; font-size: 11px; line-height: 1.7; }
  footer a { color: #60a5fa; text-decoration: none; }
  footer a:hover { text-decoration: underline; }
  .skeleton { background: linear-gradient(90deg, #1a1d29 0%, #252837 50%, #1a1d29 100%); background-size: 200% 100%; animation: shimmer 1.5s infinite; border-radius: 6px; height: 28px; margin-top: 8px; }
  @keyframes shimmer { 0% { background-position: 200% 0; } 100% { background-position: -200% 0; } }
  .empty-state { text-align: center; padding: 40px; color: #6b7280; font-style: italic; }
  .scale-toggle { display: inline-flex; background: #141822; border: 1px solid #252837; border-radius: 8px; padding: 3px; font-size: 11px; }
  .scale-toggle button { background: transparent; border: none; color: #9aa0b4; padding: 5px 10px; cursor: pointer; border-radius: 5px; font-family: inherit; }
  .scale-toggle button.active { background: #252837; color: #e8e8f0; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="brand">
      <div class="brand-mark">🎬</div>
      <div>
        <h1>V3 Cluster</h1>
        <p class="tagline">Real-time status of our distributed rendering infrastructure</p>
      </div>
    </div>
    <div class="updated">
      <div><span class="live-dot"></span>Live</div>
      <div id="lastUpdate">Last fetch: —</div>
      <div class="scale-toggle">
        <button onclick="setScale(1)" id="scale1h" class="">1h</button>
        <button onclick="setScale(24)" id="scale24h" class="active">24h</button>
        <button onclick="setScale(168)" id="scale7d" class="">7d</button>
      </div>
    </div>
  </header>

  <div class="stat-grid">
    <div class="stat-card"><div class="accent accent-green"></div>
      <div class="label">Online Nodes</div>
      <div class="value ok" id="vOnline">—</div>
      <div class="sub" id="sOnline">—</div>
    </div>
    <div class="stat-card"><div class="accent accent-blue"></div>
      <div class="label">Active Jobs</div>
      <div class="value" id="vActive">—</div>
      <div class="sub" id="sActive">—</div>
    </div>
    <div class="stat-card"><div class="accent accent-orange"></div>
      <div class="label">24h Throughput</div>
      <div class="value" id="vThroughput">—</div>
      <div class="sub" id="sThroughput">—</div>
    </div>
    <div class="stat-card"><div class="accent accent-purple"></div>
      <div class="label">Success Rate</div>
      <div class="value" id="vSuccess">—</div>
      <div class="sub" id="sSuccess">—</div>
    </div>
  </div>

  <h2>Cluster Nodes</h2>
  <div class="nodes-grid" id="nodesGrid">
    <div class="empty-state">Loading nodes…</div>
  </div>

  <h2>Activity (last <span id="windowLabel">24h</span>)</h2>
  <div class="charts-grid">
    <div class="chart-box">
      <h3>Throughput · jobs per hour</h3>
      <div class="sub-h">Total vs. successful renders</div>
      <div class="chart-canvas-wrap"><canvas id="chartThroughput"></canvas></div>
    </div>
    <div class="chart-box">
      <h3>Pipeline Mix</h3>
      <div class="sub-h">Job count per TC pipeline</div>
      <div class="chart-canvas-wrap"><canvas id="chartByTC"></canvas></div>
    </div>
    <div class="chart-box">
      <h3>Latency · p50 + p95 by pipeline</h3>
      <div class="sub-h">Render duration (seconds)</div>
      <div class="chart-canvas-wrap"><canvas id="chartLatency"></canvas></div>
    </div>
    <div class="chart-box">
      <h3>Per-Node Performance</h3>
      <div class="sub-h">Job volume + success rate per node</div>
      <div id="nodeStats" style="padding-top: 8px;"></div>
    </div>
  </div>

  <footer>
    <div><strong>V3 Cluster Status</strong> · Public view · refreshed every 15s</div>
    <div style="margin-top: 4px;">Operational metrics are reported in aggregate. Internal hostnames, IP addresses, and APIs are not exposed.</div>
  </footer>
</div>

<script>
let charts = {};
let currentScale = 24;

function fmtBytes(n) {
  if (!n) return '—';
  const u = ['B','KB','MB','GB']; let i = 0; let v = n;
  while (v >= 1024 && i < u.length-1) { v /= 1024; i++; }
  return v.toFixed(1) + ' ' + u[i];
}
function fmtSec(s) {
  if (s == null || s === 0) return '—';
  if (s < 60) return s.toFixed(1) + 's';
  if (s < 3600) return Math.floor(s/60) + 'm ' + (s%60).toFixed(0) + 's';
  return Math.floor(s/3600) + 'h ' + Math.floor((s%3600)/60) + 'm';
}
function fmtTime(epoch) { return new Date(epoch * 1000).toLocaleTimeString(); }
function fmtAgo(epoch) {
  if (!epoch) return '—';
  const dt = Math.floor(Date.now()/1000 - epoch);
  if (dt < 60) return dt + 's ago';
  if (dt < 3600) return Math.floor(dt/60) + 'm ago';
  if (dt < 86400) return Math.floor(dt/3600) + 'h ago';
  return Math.floor(dt/86400) + 'd ago';
}
function tierClass(tier) { return tier.replace(/[\s\u2013]+/g, '-'); }

const CHART_DEFAULTS = {
  responsive: true, maintainAspectRatio: false,
  animation: { duration: 600 },
  plugins: { legend: { labels: { color: '#9aa0b4', font: { size: 11 } } } },
  scales: {
    x: { ticks: { color: '#6b7280', font: { size: 10 } }, grid: { color: '#1a1d29', drawBorder: false } },
    y: { ticks: { color: '#6b7280', font: { size: 10 } }, grid: { color: '#1a1d29', drawBorder: false }, beginAtZero: true },
  },
};

function makeChart(id, type, data, options) {
  if (charts[id]) charts[id].destroy();
  charts[id] = new Chart(document.getElementById(id).getContext('2d'), { type, data, options: { ...CHART_DEFAULTS, ...(options || {}) } });
}

async function load() {
  let data;
  try {
    const r = await fetch('/api/cluster/public?hours=' + currentScale);
    data = await r.json();
  } catch (err) {
    document.getElementById('nodesGrid').innerHTML = '<div class="empty-state">⚠ Could not reach status API. Retrying…</div>';
    return;
  }
  if (!data.ok) return;
  document.getElementById('lastUpdate').textContent = 'Last fetch: ' + fmtTime(data.server_time);
  document.getElementById('windowLabel').textContent = currentScale === 1 ? '1 hour' : (currentScale === 168 ? '7 days' : '24 hours');
  renderSummary(data);
  renderNodes(data.nodes);
  renderCharts(data.metrics);
}

function renderSummary(d) {
  const s = d.summary;
  const onlinePct = s.enabled_nodes > 0 ? Math.round(100 * s.online_nodes / s.enabled_nodes) : 0;
  document.getElementById('vOnline').innerHTML = s.online_nodes + ' <span style="font-size:18px; color:#6b7280;">/ ' + s.enabled_nodes + '</span>';
  document.getElementById('sOnline').textContent = onlinePct + '% available · ' + s.disabled_nodes + ' disabled';
  document.getElementById('vActive').innerHTML = s.active_jobs + ' <span style="font-size:18px; color:#6b7280;">/ ' + s.total_capacity + '</span>';
  document.getElementById('sActive').textContent = s.total_capacity > 0 ? Math.round(100 * s.active_jobs / s.total_capacity) + '% capacity in use' : 'no capacity';
  const tot = d.metrics.totals;
  document.getElementById('vThroughput').textContent = tot.total || 0;
  document.getElementById('sThroughput').textContent = (tot.ok || 0) + ' successful · ' + (tot.fail || 0) + ' failed';
  document.getElementById('vSuccess').textContent = (tot.success_rate || 0) + '%';
  document.getElementById('sSuccess').textContent = tot.ok + '/' + tot.total + ' jobs succeeded';
}

function renderNodes(nodes) {
  const html = nodes.map(n => {
    let statusClass = 'offline', statusText = 'OFFLINE';
    if (!n.enabled) { statusClass = 'disabled'; statusText = 'DISABLED'; }
    else if (n.healthy) { statusClass = 'online'; statusText = 'ONLINE'; }
    const pct = n.max_concurrent > 0 ? (n.active_jobs / n.max_concurrent * 100) : 0;
    const isBusy = pct >= 80;
    return `
      <div class="node ${statusClass}">
        <div class="node-header">
          <div>
            <span class="node-name">${n.name}</span>
            <span class="node-tier tier-${tierClass(n.tier)}">${n.tier}</span>
          </div>
          <div><span class="node-status ns-${statusClass}">${statusText}</span></div>
        </div>
        <div class="util">
          <span>${n.active_jobs} / ${n.max_concurrent} concurrent</span>
          <span style="color:#6b7280;">${pct.toFixed(0)}% utilization</span>
        </div>
        <div class="bar"><div class="bar-fill ${isBusy ? 'busy' : ''}" style="width:${pct}%"></div></div>
        <dl class="node-meta">
          <dt>Compute</dt><dd>${n.encoder_kind}</dd>
          <dt>Last seen</dt><dd>${fmtAgo(n.last_seen_ago ? (Date.now()/1000 - n.last_seen_ago) : null)}</dd>
        </dl>
      </div>`;
  }).join('');
  document.getElementById('nodesGrid').innerHTML = html || '<div class="empty-state">No nodes configured.</div>';
}

function renderCharts(m) {
  const hours = m.hourly_throughput.map(h => {
    const d = new Date(h.hour * 1000);
    return currentScale === 168 ? (d.getMonth()+1) + '/' + d.getDate() : (d.getHours().toString().padStart(2,'0') + ':00');
  });
  makeChart('chartThroughput', 'line', {
    labels: hours,
    datasets: [
      { label: 'Total jobs', data: m.hourly_throughput.map(h => h.total), borderColor: '#60a5fa', backgroundColor: 'rgba(96,165,250,0.15)', fill: true, tension: 0.3, pointRadius: 0, borderWidth: 2 },
      { label: 'Successful', data: m.hourly_throughput.map(h => h.ok), borderColor: '#22c55e', backgroundColor: 'rgba(34,197,94,0.1)', fill: false, tension: 0.3, pointRadius: 0, borderWidth: 2 },
    ],
  });

  const tcs = m.by_tc.map(t => (t.tc || '').toUpperCase());
  const tcColors = { TC01:'#60a5fa', TC02:'#22c55e', TC03:'#f59e0b', TC04:'#a855f7', TC05:'#ec4899', TC06:'#14b8a6' };
  makeChart('chartByTC', 'doughnut', {
    labels: tcs,
    datasets: [{
      data: m.by_tc.map(t => t.total),
      backgroundColor: tcs.map(t => tcColors[t] || '#6b7280'),
      borderColor: '#0e1320', borderWidth: 2,
    }],
  }, { scales: {}, plugins: { legend: { position: 'right', labels: { color: '#9aa0b4', font: { size: 11 } } } } });

  makeChart('chartLatency', 'bar', {
    labels: tcs,
    datasets: [
      { label: 'p50', data: m.by_tc.map(t => t.p50_sec), backgroundColor: '#60a5fa', borderRadius: 4 },
      { label: 'p95', data: m.by_tc.map(t => t.p95_sec), backgroundColor: '#f59e0b', borderRadius: 4 },
    ],
  }, { scales: { ...CHART_DEFAULTS.scales, y: { ...CHART_DEFAULTS.scales.y, ticks: { ...CHART_DEFAULTS.scales.y.ticks, callback: v => v + 's' } } } });

  const stats = m.by_node || [];
  if (stats.length === 0) {
    document.getElementById('nodeStats').innerHTML = '<div class="empty-state">No data</div>';
    return;
  }
  const maxTotal = Math.max(...stats.map(s => s.total));
  document.getElementById('nodeStats').innerHTML = stats.map(s => {
    const fillClass = s.success_rate >= 90 ? 'ok' : s.success_rate >= 70 ? 'warn' : 'err';
    const barColor = { ok: 'linear-gradient(90deg,#60a5fa,#22c55e)', warn: 'linear-gradient(90deg,#fbbf24,#f59e0b)', err: 'linear-gradient(90deg,#f87171,#ef4444)' }[fillClass];
    const barWidth = (s.total / maxTotal * 100).toFixed(1);
    return `<div class="tier-bar">
      <span class="name">${s.node}</span>
      <div class="track"><div class="fill" style="width:${barWidth}%; background:${barColor};">${s.total}</div></div>
      <span class="val">${s.success_rate}% ok · ${fmtSec(s.avg_sec)} avg</span>
    </div>`;
  }).join('');
}

function setScale(hours) {
  currentScale = hours;
  document.querySelectorAll('.scale-toggle button').forEach(b => b.classList.remove('active'));
  document.getElementById('scale' + (hours === 1 ? '1h' : (hours === 24 ? '24h' : '7d'))).classList.add('active');
  load();
}

load();
setInterval(load, 15000);
</script>
</body>
</html>
"""
