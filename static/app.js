"use strict";

const $ = (id) => document.getElementById(id);

const form = $("form");
const urlInput = $("url");
const qualitySel = $("quality");
// absent when the app runs as a hosted server (no local browser to read from)
const cookiesSel = $("cookies");
const cookieValue = () => (cookiesSel ? cookiesSel.value : "");
const goBtn = $("go");
const peekBtn = $("peek");

const preview = $("preview");
const pvThumb = $("pv-thumb");
const pvTitle = $("pv-title");
const pvMeta = $("pv-meta");
const pvChips = $("pv-chips");

const jobCard = $("job");
const jobStatus = $("job-status");
const jobName = $("job-name");
const jobMeta = $("job-meta");
const bar = $("bar");
const saveLink = $("save");
const jobError = $("job-error");

let polling = null;

/* ---------- helpers ---------------------------------------------------- */

function humanBytes(n) {
  if (!n && n !== 0) return "";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v.toFixed(v < 10 && i > 0 ? 1 : 0)} ${units[i]}`;
}

function humanSpeed(bps) {
  return bps ? `${humanBytes(bps)}/s` : "";
}

function humanTime(seconds) {
  if (seconds === null || seconds === undefined) return "";
  const s = Math.max(0, Math.round(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const pad = (x) => String(x).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(sec)}` : `${m}:${pad(sec)}`;
}

async function postJSON(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  let data = {};
  try {
    data = await res.json();
  } catch (_) {
    data = { ok: false, error: `Server error (${res.status})` };
  }
  return data;
}

function setBusy(busy) {
  goBtn.disabled = busy;
  peekBtn.disabled = busy;
  goBtn.textContent = busy ? "Working…" : "Download";
}

/* ---------- URL normalisation ------------------------------------------ */

// Bare ids like "Kt7XVUzjVlE" become a YouTube Shorts link.
function normalizeUrl(raw) {
  const value = raw.trim();
  if (!value) return "";
  if (/^https?:\/\//i.test(value)) return value;
  if (/^[\w-]{11}$/.test(value)) return `https://www.youtube.com/watch?v=${value}`;
  if (/^(www\.|m\.|music\.)/i.test(value)) return `https://${value}`;
  if (/^[\w.-]+\.[a-z]{2,}(\/|$)/i.test(value)) return `https://${value}`;
  return value;
}

/* ---------- preview ---------------------------------------------------- */

peekBtn.addEventListener("click", async () => {
  const url = normalizeUrl(urlInput.value);
  if (!url) {
    urlInput.focus();
    return;
  }
  urlInput.value = url;

  peekBtn.disabled = true;
  peekBtn.textContent = "Fetching…";
  try {
    const data = await postJSON("/info", { url });
    if (!data.ok) {
      showError(data.error || "Could not read that link.");
      preview.classList.add("hidden");
      return;
    }
    pvThumb.src = data.thumbnail || "";
    pvThumb.style.display = data.thumbnail ? "" : "none";
    pvTitle.textContent = data.title || "(no title)";
    const bits = [];
    if (data.uploader) bits.push(data.uploader);
    if (data.duration) bits.push(humanTime(data.duration));
    if (data.is_live) bits.push("LIVE");
    pvMeta.textContent = bits.join(" · ");
    pvChips.innerHTML = "";
    (data.heights || []).slice(0, 8).forEach((h) => {
      const chip = document.createElement("span");
      chip.className = "chip";
      chip.textContent = `${h}p`;
      pvChips.appendChild(chip);
    });
    preview.classList.remove("hidden");
    jobError.classList.add("hidden");
  } finally {
    peekBtn.disabled = false;
    peekBtn.textContent = "Preview";
  }
});

/* ---------- download --------------------------------------------------- */

form.addEventListener("submit", async (event) => {
  event.preventDefault();

  const url = normalizeUrl(urlInput.value);
  if (!url) {
    urlInput.focus();
    return;
  }
  urlInput.value = url;

  if (polling) clearInterval(polling);
  jobError.classList.add("hidden");
  saveLink.classList.add("hidden");
  jobName.textContent = "";
  jobMeta.textContent = "";
  bar.classList.remove("indeterminate");
  bar.style.width = "0%";
  jobStatus.textContent = "starting";
  jobStatus.className = "status";
  jobCard.classList.remove("hidden");

  setBusy(true);
  const data = await postJSON("/download", {
    url,
    quality: qualitySel.value,
    cookies_browser: cookieValue(),
  });
  setBusy(false);

  if (!data.ok) {
    jobStatus.textContent = "error";
    jobStatus.className = "status err";
    showError(data.error || "Could not start the download.");
    return;
  }

  poll(data.job_id);
});

function poll(jobId) {
  bar.classList.add("indeterminate");

  polling = setInterval(async () => {
    let data;
    try {
      const res = await fetch(`/progress/${jobId}`);
      data = await res.json();
    } catch (_) {
      return; // transient network blip, try again on the next tick
    }

    if (!data.ok) {
      clearInterval(polling);
      polling = null;
      jobStatus.textContent = "expired";
      jobStatus.className = "status err";
      showError(data.error || "This job is no longer available.");
      return;
    }

    const status = data.status;

    if (status === "downloading") {
      bar.classList.remove("indeterminate");
      jobStatus.textContent = "downloading";
      jobStatus.className = "status";
      if (typeof data.percent === "number") {
        bar.style.width = `${data.percent}%`;
      } else {
        bar.classList.add("indeterminate");
      }
      const parts = [];
      if (typeof data.percent === "number") parts.push(`${data.percent}%`);
      if (data.total_bytes) parts.push(`${humanBytes(data.downloaded_bytes || 0)} / ${humanBytes(data.total_bytes)}`);
      if (data.speed) parts.push(humanSpeed(data.speed));
      if (data.eta) parts.push(`ETA ${humanTime(data.eta)}`);
      jobMeta.textContent = parts.join(" · ");
      if (data.filename) jobName.textContent = data.filename;
      return;
    }

    if (status === "processing") {
      bar.classList.remove("indeterminate");
      bar.style.width = "100%";
      jobStatus.textContent = "processing";
      jobStatus.className = "status";
      jobMeta.textContent = "Downloaded — merging / converting with ffmpeg…";
      jobName.textContent = data.filename || jobName.textContent;
      return;
    }

    if (status === "done") {
      clearInterval(polling);
      polling = null;
      bar.classList.remove("indeterminate");
      bar.style.width = "100%";
      jobStatus.textContent = "done";
      jobStatus.className = "status ok";
      jobName.textContent = data.filename || "video";
      jobMeta.textContent = data.filesize ? humanBytes(data.filesize) : "";

      saveLink.href = `/file/${jobId}`;
      saveLink.setAttribute("download", data.filename || "video");
      saveLink.classList.remove("hidden");

      // kick off the browser download automatically
      saveLink.click();
      return;
    }

    if (status === "error") {
      clearInterval(polling);
      polling = null;
      bar.classList.remove("indeterminate");
      bar.style.width = "0%";
      jobStatus.textContent = "error";
      jobStatus.className = "status err";
      showError(data.error || "The download failed.");
    }
  }, 700);
}

function showError(message) {
  jobCard.classList.remove("hidden");
  jobError.textContent = message;
  jobError.classList.remove("hidden");
}

/* ---------- niceties --------------------------------------------------- */

urlInput.addEventListener("paste", (event) => {
  const text = (event.clipboardData || window.clipboardData).getData("text");
  if (!text) return;
  event.preventDefault();
  urlInput.value = normalizeUrl(text);
});
