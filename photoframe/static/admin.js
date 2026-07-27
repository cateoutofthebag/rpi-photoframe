"use strict";

const $ = (selector) => document.querySelector(selector);

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error || `${response.status} ${response.statusText}`);
  return body;
}

/* ------------------------------------------------------------------ status */

function describeStatus(status) {
  if (status.quiet) return "Screen off (quiet hours)";
  if (!status.photo_count) return "No photos yet — add some below";
  const parts = [];
  parts.push(status.paused ? "Paused" : "Playing");
  if (status.photo_filename) parts.push(status.photo_filename);
  const weather = status.weather;
  if (weather && weather.temperature != null) {
    parts.push(`${Math.round(weather.temperature)}${weather.unit} ${weather.text || ""}`.trim());
  }
  if (status.overlay_visible) parts.push("info showing");
  return parts.join(" · ");
}

async function pollStatus() {
  const label = $("#status");
  try {
    const status = await api("/api/status");
    label.textContent = describeStatus(status);
    label.classList.remove("offline");
    $("#pause-btn").textContent = status.paused ? "Play" : "Pause";
    $("#photo-count").textContent = status.photo_count;
  } catch (err) {
    label.textContent = `Frame unreachable — ${err.message}`;
    label.classList.add("offline");
  }
}

/* ---------------------------------------------------------------- controls */

document.querySelectorAll("[data-action]").forEach((button) => {
  button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      await api("/api/control", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: button.dataset.action }),
      });
      setTimeout(pollStatus, 400);
    } catch (err) {
      $("#status").textContent = err.message;
    } finally {
      button.disabled = false;
    }
  });
});

/* ------------------------------------------------------------------ photos */

async function loadPhotos() {
  const grid = $("#grid");
  try {
    const { photos } = await api("/api/photos");
    $("#photo-count").textContent = photos.length;
    if (!photos.length) {
      grid.innerHTML = '<p class="muted">No photos yet.</p>';
      return;
    }
    grid.innerHTML = "";
    // Newest first here — the opposite of slideshow order, but it's what you
    // want to see right after uploading.
    photos.reverse().forEach((photo) => grid.appendChild(tileFor(photo)));
  } catch (err) {
    grid.innerHTML = `<p class="muted">Could not load photos: ${err.message}</p>`;
  }
}

function tileFor(photo) {
  const tile = document.createElement("div");
  tile.className = "tile";

  const img = document.createElement("img");
  img.src = `/thumbs/${photo.id}.jpg`;
  img.alt = photo.filename;
  img.loading = "lazy";
  tile.appendChild(img);

  const remove = document.createElement("button");
  remove.type = "button";
  remove.textContent = "Delete";
  remove.title = `Delete ${photo.filename}`;
  remove.addEventListener("click", async () => {
    if (!confirm(`Delete ${photo.filename}?`)) return;
    remove.disabled = true;
    try {
      await api(`/api/photos/${photo.id}`, { method: "DELETE" });
      tile.remove();
      $("#photo-count").textContent = document.querySelectorAll(".tile").length;
    } catch (err) {
      alert(err.message);
      remove.disabled = false;
    }
  });
  tile.appendChild(remove);
  return tile;
}

$("#refresh-btn").addEventListener("click", loadPhotos);

/* ------------------------------------------------------------------ upload */

const dropzone = $("#dropzone");
const fileInput = $("#file-input");

["dragenter", "dragover"].forEach((event) =>
  dropzone.addEventListener(event, (e) => {
    e.preventDefault();
    dropzone.classList.add("hover");
  })
);

["dragleave", "drop"].forEach((event) =>
  dropzone.addEventListener(event, (e) => {
    e.preventDefault();
    dropzone.classList.remove("hover");
  })
);

dropzone.addEventListener("drop", (e) => {
  if (e.dataTransfer?.files?.length) uploadAll([...e.dataTransfer.files]);
});

fileInput.addEventListener("change", () => {
  if (fileInput.files.length) uploadAll([...fileInput.files]);
  fileInput.value = "";
});

function uploadOne(file, onProgress) {
  // XHR rather than fetch: it reports upload progress, which matters when
  // you're pushing a dozen phone photos over wifi to a Pi Zero.
  return new Promise((resolve, reject) => {
    const form = new FormData();
    form.append("photos", file);
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/photos");
    xhr.upload.addEventListener("progress", (e) => {
      if (e.lengthComputable) onProgress(e.loaded / e.total);
    });
    xhr.addEventListener("load", () => {
      let body = {};
      try { body = JSON.parse(xhr.responseText); } catch { /* keep the status text */ }
      if (xhr.status >= 200 && xhr.status < 300) resolve(body);
      else reject(new Error(body.error || body.errors?.[0]?.error || `HTTP ${xhr.status}`));
    });
    xhr.addEventListener("error", () => reject(new Error("Network error")));
    xhr.send(form);
  });
}

async function uploadAll(files) {
  const progress = $("#upload-progress");
  const fill = $("#progress-fill");
  const label = $("#progress-label");
  const errors = $("#upload-errors");
  errors.innerHTML = "";
  progress.hidden = false;

  let done = 0;
  for (const file of files) {
    label.textContent = `Uploading ${file.name} (${done + 1} of ${files.length})`;
    try {
      const result = await uploadOne(file, (fraction) => {
        fill.style.width = `${((done + fraction) / files.length) * 100}%`;
      });
      (result.errors || []).forEach((entry) => addError(`${entry.filename}: ${entry.error}`));
    } catch (err) {
      addError(`${file.name}: ${err.message}`);
    }
    done += 1;
    fill.style.width = `${(done / files.length) * 100}%`;
  }

  label.textContent = `Uploaded ${done} file${done === 1 ? "" : "s"}`;
  setTimeout(() => {
    progress.hidden = true;
    fill.style.width = "0";
  }, 2500);
  loadPhotos();
}

function addError(message) {
  const item = document.createElement("li");
  item.textContent = message;
  $("#upload-errors").appendChild(item);
}

/* ----------------------------------------------------------------- sources */

const SECRET = "__unchanged__";

const SOURCE_TYPES = {
  folder: {
    label: "Watched folder",
    hint: "Any directory on the Pi — a USB stick, an OS-level mount, or a folder kept in step by rclone or Syncthing.",
    fields: [{ key: "path", label: "Folder path", placeholder: "/home/pi/photos" }],
  },
  smb: {
    label: "SMB / network share",
    hint: "Talks SMB directly, so there's nothing to mount and a NAS that's switched off can't hold up boot.",
    fields: [
      { key: "server", label: "Server", placeholder: "nas.local" },
      { key: "share", label: "Share", placeholder: "photos" },
      { key: "path", label: "Folder within the share", placeholder: "family/2026", optional: true },
      { key: "username", label: "Username", optional: true },
      { key: "password", label: "Password", type: "password", optional: true },
      { key: "domain", label: "Domain", optional: true },
      { key: "port", label: "Port", type: "number" },
    ],
  },
  photoprism: {
    label: "PhotoPrism album",
    hint: "An app password is easiest. Photos come through the thumbnailer, already sized and rotated for the screen.",
    fields: [
      { key: "url", label: "Server URL", placeholder: "http://photoprism.local:2342" },
      { key: "album", label: "Album (title or UID)", placeholder: "Holidays" },
      { key: "token", label: "App password", type: "password", optional: true },
      { key: "username", label: "Username", optional: true },
      { key: "password", label: "Password", type: "password", optional: true },
      { key: "size", label: "Size", type: "select",
        options: ["fit_720", "fit_1280", "fit_1600", "fit_1920", "fit_2048", "fit_2560", "fit_3840", "original"] },
      { key: "verify_tls", label: "Verify the TLS certificate", type: "checkbox" },
    ],
  },
  icloud: {
    label: "iCloud shared album",
    hint: "Needs a public share link: in Photos, share the album and turn on Public Website. Unofficial API — Apple could change it.",
    fields: [{ key: "url", label: "Shared album link", placeholder: "https://www.icloud.com/sharedalbum/#B0..." }],
  },
};

let sources = [];

async function loadSources() {
  try {
    const config = await api("/api/config");
    sources = config.sources || [];
    renderSources();
    refreshSourceStatus();
  } catch (err) {
    $("#sources").innerHTML = `<p class="muted">Could not load sources: ${err.message}</p>`;
  }
}

function renderSources() {
  const host = $("#sources");
  host.innerHTML = "";
  if (!sources.length) {
    host.innerHTML = '<p class="muted small">No network sources yet.</p>';
    return;
  }
  sources.forEach((source, index) => host.appendChild(sourceCard(source, index)));
}

function sourceCard(source, index) {
  const spec = SOURCE_TYPES[source.type] || { label: source.type, fields: [] };
  const card = document.createElement("fieldset");
  card.className = "source";
  card.dataset.index = index;

  const legend = document.createElement("legend");
  legend.textContent = spec.label;
  card.appendChild(legend);

  if (spec.hint) {
    const hint = document.createElement("p");
    hint.className = "muted small";
    hint.textContent = spec.hint;
    card.appendChild(hint);
  }

  const statusLine = document.createElement("p");
  statusLine.className = "source-status muted small";
  statusLine.dataset.status = source.id;
  card.appendChild(statusLine);

  card.appendChild(sourceField({ key: "name", label: "Name" }, source));
  spec.fields.forEach((field) => card.appendChild(sourceField(field, source)));

  const row = document.createElement("div");
  row.className = "row";
  row.appendChild(sourceField({ key: "interval_minutes", label: "Check every (min)", type: "number" }, source));
  row.appendChild(sourceField({ key: "limit", label: "Max photos", type: "number" }, source));
  card.appendChild(row);

  card.appendChild(sourceField({ key: "enabled", label: "Enabled", type: "checkbox" }, source));
  card.appendChild(sourceField(
    { key: "remove_deleted", label: "Remove photos deleted upstream", type: "checkbox" }, source));

  const actions = document.createElement("div");
  actions.className = "controls";
  actions.appendChild(sourceButton("Test", "ghost small", () => testSource(source.id, statusLine)));
  actions.appendChild(sourceButton("Sync now", "ghost small", () => syncSource(source.id, statusLine)));
  actions.appendChild(sourceButton("Remove", "ghost small danger", () => {
    if (!confirm(`Remove "${source.name}"? Its photos will be deleted from the frame when you save.`)) return;
    sources.splice(index, 1);
    renderSources();
  }));
  card.appendChild(actions);
  return card;
}

function sourceButton(text, className, onClick) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = className;
  button.textContent = text;
  button.addEventListener("click", onClick);
  return button;
}

function sourceField(field, source) {
  const label = document.createElement("label");
  const value = source[field.key];

  let input;
  if (field.type === "select") {
    input = document.createElement("select");
    field.options.forEach((option) => {
      const element = document.createElement("option");
      element.value = option;
      element.textContent = option;
      element.selected = option === value;
      input.appendChild(element);
    });
  } else {
    input = document.createElement("input");
    input.type = field.type || "text";
    if (field.type === "checkbox") input.checked = value !== false;
    else input.value = value ?? "";
    if (field.placeholder) input.placeholder = field.placeholder;
  }

  input.dataset.key = field.key;
  if (field.type === "checkbox") {
    label.className = "check";
    label.appendChild(input);
    label.appendChild(document.createTextNode(` ${field.label}`));
  } else {
    label.textContent = field.label + (field.optional ? " (optional)" : "");
    label.appendChild(input);
  }
  return label;
}

function collectSources() {
  return [...document.querySelectorAll("#sources .source")].map((card) => {
    const source = { ...sources[Number(card.dataset.index)] };
    card.querySelectorAll("[data-key]").forEach((input) => {
      source[input.dataset.key] = readValue(input);
    });
    return source;
  });
}

async function testSource(id, line) {
  line.textContent = "Testing…";
  line.className = "source-status muted small";
  try {
    const result = await api(`/api/sources/${id}/test`, { method: "POST" });
    line.textContent = result.message;
    line.className = `source-status small ${result.ok ? "ok" : "bad"}`;
  } catch (err) {
    line.textContent = err.message;
    line.className = "source-status small bad";
  }
}

async function syncSource(id, line) {
  line.textContent = "Sync queued…";
  try {
    await api(`/api/sources/${id}/sync`, { method: "POST" });
    setTimeout(refreshSourceStatus, 2000);
  } catch (err) {
    line.textContent = err.message;
    line.className = "source-status small bad";
  }
}

async function refreshSourceStatus() {
  try {
    const { sources: rows } = await api("/api/sources");
    rows.forEach((row) => {
      const line = document.querySelector(`[data-status="${row.id}"]`);
      if (!line) return;
      if (row.last_error) {
        line.textContent = `Last sync failed: ${row.last_error}`;
        line.className = "source-status small bad";
      } else if (row.syncing) {
        line.textContent = "Syncing now…";
        line.className = "source-status small";
      } else {
        const when = row.last_run ? new Date(row.last_run * 1000).toLocaleTimeString() : "never";
        line.textContent = `${row.photos} photo(s) · last checked ${when}`;
        line.className = "source-status muted small";
      }
    });
  } catch { /* the save note surfaces connection trouble */ }
}

$("#add-source-btn").addEventListener("click", () => {
  const type = $("#source-type").value;
  sources.push({
    id: `new-${Date.now().toString(36)}`,
    type,
    name: SOURCE_TYPES[type].label,
    enabled: true,
    interval_minutes: 30,
    limit: 500,
    remove_deleted: true,
    verify_tls: true,
    port: 445,
    size: "fit_1920",
  });
  renderSources();
});

$("#sync-all-btn").addEventListener("click", async () => {
  try {
    await api("/api/sync", { method: "POST" });
    $("#save-note").textContent = "Sync started.";
    setTimeout(refreshSourceStatus, 2000);
  } catch (err) {
    $("#save-note").textContent = err.message;
  }
});

setInterval(refreshSourceStatus, 15000);

/* ---------------------------------------------------------------- settings */

function setByPath(target, path, value) {
  const keys = path.split(".");
  const last = keys.pop();
  let node = target;
  for (const key of keys) node = node[key] ??= {};
  node[last] = value;
}

function readValue(input) {
  if (input.type === "checkbox") return input.checked;
  if (input.type === "number") return input.value === "" ? 0 : Number(input.value);
  if (input.tagName === "SELECT" && /^-?\d+$/.test(input.value)) return Number(input.value);
  return input.value;
}

$("#settings").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("#save-btn");
  const note = $("#save-note");
  const patch = {};
  document.querySelectorAll("[data-path]").forEach((input) => {
    setByPath(patch, input.dataset.path, readValue(input));
  });
  patch.sources = collectSources();

  button.disabled = true;
  note.textContent = "Saving…";
  try {
    await api("/api/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    });
    note.textContent = "Saved — the frame picks this up immediately.";
    loadSources();  // pick up server-assigned ids and drop any it rejected
  } catch (err) {
    note.textContent = `Failed: ${err.message}`;
  } finally {
    button.disabled = false;
    setTimeout(() => (note.textContent = ""), 4000);
  }
});

$("#locate-btn").addEventListener("click", () => {
  const note = $("#save-note");
  if (!navigator.geolocation) {
    note.textContent = "This browser has no location support.";
    return;
  }
  note.textContent = "Locating…";
  navigator.geolocation.getCurrentPosition(
    ({ coords }) => {
      document.querySelector('[data-path="weather.latitude"]').value = coords.latitude.toFixed(4);
      document.querySelector('[data-path="weather.longitude"]').value = coords.longitude.toFixed(4);
      note.textContent = "Location filled in — press Save.";
    },
    (err) => (note.textContent = `Location failed: ${err.message}`)
  );
});

/* -------------------------------------------------------------------- boot */

loadPhotos();
loadSources();
pollStatus();
setInterval(pollStatus, 5000);
