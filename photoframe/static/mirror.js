"use strict";

/* Long-polls /api/frame.jpg: each request parks on the server until the
   picture actually changes, so the mirror updates the moment the frame does
   and sits almost silent in between. */

const img = document.getElementById("frame");
const empty = document.getElementById("empty");
const dot = document.getElementById("dot");
const stateLabel = document.getElementById("state");

let generation = null;
let objectUrl = null;
let failures = 0;

function setState(text, level) {
  stateLabel.textContent = text;
  dot.className = `dot ${level || ""}`;
}

async function pump() {
  for (;;) {
    const query = generation === null ? "" : `?since=${generation}`;
    try {
      const response = await fetch(`/api/frame.jpg${query}`, { cache: "no-store" });

      if (response.status === 304) {
        failures = 0;
        continue; // nothing changed while we waited; ask again
      }
      if (response.status === 503) {
        setState("frame not running", "down");
        await sleep(3000);
        continue;
      }
      if (response.status === 403) {
        setState("mirror disabled in settings", "down");
        return;
      }
      if (!response.ok) throw new Error(`HTTP ${response.status}`);

      generation = Number(response.headers.get("X-Frame-Generation") || 0);
      await show(await response.blob());
      failures = 0;
      setState("live", "");
    } catch (err) {
      failures += 1;
      setState(`reconnecting… (${err.message})`, failures > 2 ? "down" : "stale");
      await sleep(Math.min(10000, 1000 * failures));
    }
  }
}

function show(blob) {
  // Decode before swapping so the picture never flashes through white.
  return new Promise((resolve) => {
    const next = URL.createObjectURL(blob);
    const preload = new Image();
    preload.onload = () => {
      img.src = next;
      img.hidden = false;
      empty.hidden = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
      objectUrl = next;
      resolve();
    };
    preload.onerror = () => {
      URL.revokeObjectURL(next);
      resolve();
    };
    preload.src = next;
  });
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/* ---------------------------------------------------------------- controls */

document.querySelectorAll("[data-action]").forEach((button) => {
  button.addEventListener("click", async () => {
    try {
      await fetch("/api/control", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: button.dataset.action }),
      });
    } catch { /* the status line will show the connection is unhappy */ }
  });
});

document.getElementById("fit-btn").addEventListener("click", (event) => {
  document.body.classList.toggle("fill");
  event.target.textContent = document.body.classList.contains("fill") ? "Fit" : "Fill";
});

document.getElementById("full-btn").addEventListener("click", () => {
  if (document.fullscreenElement) document.exitFullscreen();
  else document.documentElement.requestFullscreen().catch(() => {});
});

/* Hide the chrome when idle so this can be left up on a spare monitor. */
let idleTimer;
function wake() {
  document.body.classList.remove("idle");
  clearTimeout(idleTimer);
  idleTimer = setTimeout(() => document.body.classList.add("idle"), 3000);
}
["mousemove", "touchstart", "keydown", "click"].forEach((event) =>
  document.addEventListener(event, wake, { passive: true })
);
wake();

/* Keep the pause button honest, cheaply. */
setInterval(async () => {
  try {
    const status = await (await fetch("/api/status")).json();
    document.getElementById("pause-btn").textContent = status.paused ? "Play" : "Pause";
  } catch { /* ignore */ }
}, 5000);

pump();
