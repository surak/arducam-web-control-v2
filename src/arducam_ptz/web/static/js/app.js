/*
 * arducam-ptz interface.
 *
 * The whole file is one loop: read state from the API, draw it, and let the user write
 * commands back. Three rules keep it honest, and each one exists because the old page
 * broke it:
 *
 * 1. **Never rebuild the DOM.** Every readout is updated by assigning textContent or a
 *    style on a node that already exists. Re-rendering innerHTML on every poll is what
 *    made the old page drop text selections, re-layout mid-drag and zoom on touch.
 * 2. **The server is the source of truth, the UI is optimistic.** A drag or a slider
 *    paints immediately, then the reply from the server (which reports any clamping it
 *    applied) corrects the display. The UI never argues with the hardware.
 * 3. **Polling never fights the user.** While the pad or the slider is in use, incoming
 *    state updates do not overwrite what the user is doing.
 */

import { ApiError, api, pollJob, token, withToken } from "./api.js";
import { Pad } from "./pad.js";

const $ = (id) => document.getElementById(id);

const el = {
  connDot: $("conn-dot"),
  streamMeta: $("stream-meta"),
  stream: $("stream"),
  overlay: $("stream-overlay"),
  overlayText: $("stream-overlay-text"),
  reconnect: $("btn-reconnect"),
  hud: $("hud"),
  marker: $("af-marker"),

  ptzReadout: $("ptz-readout"),
  pad: $("pad"),
  padKnob: $("pad-knob"),
  center: $("btn-center"),
  release: $("btn-release"),

  focusReadout: $("focus-readout"),
  slider: $("focus-slider"),
  focusNear: $("focus-near"),
  focusFar: $("focus-far"),
  af: $("btn-af"),
  afProgress: $("af-progress"),
  afBar: $("af-progress-bar"),
  afLabel: $("af-progress-label"),
  tapAf: $("tap-af"),

  shutter: $("btn-shutter"),
  galleryBtn: $("btn-gallery"),
  photoCount: $("photo-count"),

  gallery: $("gallery"),
  galleryGrid: $("gallery-grid"),
  galleryClose: $("btn-gallery-close"),
  scrim: $("scrim"),

  // Full-size photo viewer, opened by tapping a tile in the grid.
  viewer: $("viewer"),
  viewerImg: $("viewer-img"),
  viewerName: $("viewer-name"),
  viewerMeta: $("viewer-meta"),
  viewerOpen: $("viewer-open"),
  viewerDownload: $("viewer-download"),
  viewerDelete: $("viewer-delete"),
  viewerPrev: $("viewer-prev"),
  viewerNext: $("viewer-next"),
  viewerClose: $("viewer-close"),

  help: $("help"),
  helpBtn: $("btn-help"),
  helpClose: $("btn-help-close"),
  fullscreen: $("btn-fullscreen"),
  toasts: $("toasts"),
};

/* ------------------------------------------------------------------ state */

const state = {
  online: false,
  health: null,
  stream: null,
  ptz: { pan: null, tilt: null, clamps: [] },
  focus: { position: null, min: 0, max: 4095, limits_known: false },
  // Normalised {x, y, width, height} of the area to autofocus on. The field names are
  // the API's (models.Region) on purpose: they travel to the server verbatim, and a
  // short local spelling here is exactly how tap-to-focus came back as a 422.
  region: null,
  afJob: null,
  photos: 0,
};

const NUDGE_DEGREES = 2.2; // per pad tick at full deflection
const FOCUS_STEP = 16; // per near/far press or tap on the slider track

/* ------------------------------------------------------------------ toasts */

let toastTimer = null;

function toast(message, kind = "") {
  const node = document.createElement("div");
  node.className = `toast ${kind ? `is-${kind}` : ""}`;
  node.textContent = message;
  el.toasts.replaceChildren(node);
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.remove(), kind === "error" ? 6000 : 2600);
}

function reportError(error) {
  if (error instanceof ApiError) {
    // The server's `code` is intended for programs, `detail` for humans; show both when
    // they differ, because "hardware_error" alone tells the user nothing.
    const text = error.code && error.detail && error.code !== error.detail
      ? `${error.code}: ${error.detail}`
      : error.detail || error.code;
    toast(text, "error");
    return;
  }
  toast(String(error && error.message ? error.message : error), "error");
}

/* ------------------------------------------------------------------ drawing */

function fmt(value, digits = 1) {
  return value === null || value === undefined ? "—" : Number(value).toFixed(digits);
}

function renderPtz() {
  const { pan, tilt } = state.ptz;
  if (!pan || !tilt) {
    el.ptzReadout.textContent = "—";
    return;
  }
  const held = pan.released ? " · released" : "";
  el.ptzReadout.textContent = `${fmt(pan.angle)}° / ${fmt(tilt.angle)}°${held}`;
}

function renderFocus() {
  const { position, min, max } = state.focus;
  el.focusReadout.textContent = position === null ? "—" : `${position} (${min}–${max})`;
  if (!sliderBusy) el.slider.value = String(position ?? min);
}

function renderStreamMeta() {
  const stats = state.stream;
  if (!stats) {
    el.streamMeta.textContent = "connecting…";
    return;
  }
  const parts = [`${fmt(stats.fps, 1)} fps`];
  if (stats.subscribers) parts.push(`${stats.subscribers} viewer${stats.subscribers === 1 ? "" : "s"}`);
  if (stats.dropped_frames) parts.push(`${stats.dropped_frames} dropped`);
  el.streamMeta.textContent = parts.join(" · ");

  const age = stats.last_frame_age_s;
  if (age !== null && age !== undefined && age > 3) {
    el.connDot.className = "dot is-stale";
  } else if (state.online) {
    el.connDot.className = "dot is-live";
  }
}

function renderHud() {
  const bits = [];
  if (state.focus.position !== null) bits.push(`focus ${state.focus.position}`);
  if (state.ptz.pan) bits.push(`pan ${fmt(state.ptz.pan.angle)}°  tilt ${fmt(state.ptz.tilt.angle)}°`);
  if (state.afJob && state.afJob.status === "running") bits.push(`AF ${Math.round(state.afJob.progress * 100)}%`);
  el.hud.textContent = bits.join("   ");
}

function renderAll() {
  renderPtz();
  renderFocus();
  renderStreamMeta();
  renderHud();
}

/* ------------------------------------------------------------------ stream */

let streamStarted = 0;
let streamWatchdog = null;

function startStream() {
  el.overlay.hidden = true;
  streamStarted = Date.now();
  el.stream.src = api.streamUrl();
}

function stopStream(reason) {
  el.stream.removeAttribute("src");
  el.overlay.hidden = false;
  el.overlayText.textContent = reason;
  el.connDot.className = "dot is-down";
}

el.stream.addEventListener("error", () => {
  // The Multipart stream dies with the TCP connection; every failure looks like this.
  stopStream("stream stopped responding");
  // One automatic retry, then leave it to the user: endlessly reopening the camera
  // would hammer the sensor.
  setTimeout(() => {
    if (!el.overlay.hidden && Date.now() - streamStarted > 3000 && state.online) startStream();
  }, 1200);
});

el.reconnect.addEventListener("click", startStream);

/* ------------------------------------------------------------------ polling */

async function refreshState() {
  try {
    const data = await api.state();
    state.online = true;
    state.health = data.health;
    state.stream = data.stream;
    // Never overwrite what the user is currently dragging.
    if (!pad.isActive) state.ptz = data.ptz;
    if (!sliderBusy) state.focus = { ...state.focus, ...data.focus };
    renderAll();
  } catch (error) {
    state.online = false;
    el.connDot.className = "dot is-down";
    el.streamMeta.textContent = "server unreachable";
    if (!(error instanceof ApiError && error.code === "network")) reportError(error);
  }
}

let pollTimer = null;

function schedulePolling() {
  clearInterval(pollTimer);
  // 1.5 s is brisk enough to feel live and light enough for a Pi Zero-era CPU to serve
  // alongside the MJPEG stream. Hidden tabs stop polling entirely.
  pollTimer = setInterval(() => {
    if (document.hidden) return;
    refreshState();
  }, 1500);
}

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refreshState();
});

/* Stream watchdog: /state carries the frame statistics, so if frames stop arriving the
   page notices without needing to poll the stream itself. */
streamWatchdog = setInterval(() => {
  const age = state.stream?.last_frame_age_s;
  if (age === null || age === undefined) return;
  if (age > 5 && el.overlay.hidden && state.online) {
    // The server is answering but no frames are arriving: the MJPEG connection is a
    // zombie. Closing it makes the error handler reopen the stream.
    stopStream("stream stalled - reconnecting…");
  }
}, 4000);

/* ------------------------------------------------------------------ pan / tilt */

let ptzInFlight = false;
let pendingVector = null;

async function sendNudge(vector) {
  // Drop intermediate vectors rather than queueing them: the newest direction is the
  // only one that matters, and the server coalesces consecutive nudges anyway.
  if (ptzInFlight) {
    pendingVector = vector;
    return;
  }
  ptzInFlight = true;
  try {
    // Screen-space steering vector -> servo-space deltas. This is the one place that
    // translation happens, and the signs are not symmetric:
    //
    //   pan  increases to the *right*   -> +x passes through unchanged
    //   tilt increases *downwards*      -> +y (pad up, the pad paints an upward knob
    //                                      travel for it) must be negated
    //
    // Both directions are the original rig's, taken from the app the hardware was
    // calibrated against: there, ▲ was tilt-1 and ▼ was tilt+1, and the toast said
    // "Tilt down" for a positive delta. Sending +y straight through made the pad and
    // the arrow keys steer the camera the wrong way vertically - visibly wrong on a
    // live image, and invisible in any test that only checks that *something* moved.
    const result = await api.ptz.nudge(vector.x * NUDGE_DEGREES, -vector.y * NUDGE_DEGREES);
    state.ptz = result;
    renderPtz();
    renderHud();
  } catch (error) {
    reportError(error);
  } finally {
    ptzInFlight = false;
    if (pendingVector) {
      const next = pendingVector;
      pendingVector = null;
      sendNudge(next);
    }
  }
}

const pad = new Pad(el.pad, {
  knob: el.padKnob,
  onVector: (vector) => {
    if (vector.x === 0 && vector.y === 0) return;
    sendNudge(vector);
  },
  onTap: (vector) => sendNudge({ x: vector.x * 0.5, y: vector.y * 0.5 }),
});

el.center.addEventListener("click", async () => {
  try {
    state.ptz = await api.ptz.center();
    renderAll();
  } catch (error) {
    reportError(error);
  }
});

el.release.addEventListener("click", async () => {
  try {
    state.ptz = await api.ptz.release();
    renderAll();
    toast("servos released - the camera can be moved by hand");
  } catch (error) {
    reportError(error);
  }
});

/* ------------------------------------------------------------------ focus */

let focusSendTimer = null;
let focusQueued = null;
/* True from the moment the user grabs the slider until they let go: incoming polls must
   not yank the thumb out from under their finger. `:active` is not dependable for range
   inputs (particularly with touch), so this is tracked explicitly. */
let sliderBusy = false;
el.slider.addEventListener("pointerdown", () => (sliderBusy = true));
el.slider.addEventListener("keydown", () => (sliderBusy = true));
for (const type of ["pointerup", "pointercancel", "keyup", "blur"]) {
  el.slider.addEventListener(type, () => (sliderBusy = false));
}

async function setFocus(position, { silent = false } = {}) {
  const clamped = Math.max(state.focus.min, Math.min(state.focus.max, Math.round(position)));
  state.focus.position = clamped; // optimistic: the reply corrects it if the rail clamps
  renderFocus();
  renderHud();
  try {
    const result = await api.focus.set(clamped);
    state.focus = { ...state.focus, ...result };
    renderFocus();
  } catch (error) {
    if (!silent) reportError(error);
  }
}

el.slider.addEventListener("input", () => {
  const value = Number(el.slider.value);
  state.focus.position = value;
  el.focusReadout.textContent = `${value} (${state.focus.min}–${state.focus.max})`;
  // Coalesce while dragging: at most one write per 90 ms, latest value wins.
  clearTimeout(focusSendTimer);
  focusSendTimer = setTimeout(() => {
    const queued = focusQueued ?? value;
    focusQueued = null;
    setFocus(queued);
  }, 90);
});

el.slider.addEventListener("change", () => {
  clearTimeout(focusSendTimer);
  focusQueued = null;
  setFocus(Number(el.slider.value));
});

/**
 * Wire-space focus delta for one step.
 *
 * "Near" and "far" are *physical* directions; the raw number that moves the lens there
 * depends on how it is geared, and the backend reports which way round it is
 * (``focus.near_is_high``). Hardcoding one direction is the classic silent bug here: the
 * buttons keep working and the lens drives away from the subject.
 *
 * @param {boolean} towardsNear - true for a step towards the camera.
 * @returns {number} signed change to apply to the raw focus position.
 */
function focusDelta(towardsNear) {
  const towardsHigh = towardsNear === (state.focus.near_is_high !== false);
  return towardsHigh ? FOCUS_STEP : -FOCUS_STEP;
}

el.focusNear.addEventListener("click", () => setFocus((state.focus.position ?? 0) + focusDelta(true)));
el.focusFar.addEventListener("click", () => setFocus((state.focus.position ?? 0) + focusDelta(false)));

let afAbort = null;

async function runAutofocus(region) {
  if (state.afJob && state.afJob.status === "running") {
    try {
      state.afJob = await api.focus.cancel(state.afJob.id);
      toast("autofocus cancelled");
    } catch (error) {
      reportError(error);
    }
    return;
  }

  el.afProgress.hidden = false;
  el.afBar.style.width = "0%";
  el.afLabel.textContent = "autofocus…";
  el.af.textContent = "Stop";
  const controller = new AbortController();
  afAbort = controller;
  try {
    const job = await api.focus.autofocus(region || undefined);
    state.afJob = job;
    const finished = await pollJob(job.id, {
      signal: controller.signal,
      onUpdate: (update) => {
        state.afJob = update;
        el.afBar.style.width = `${Math.round(update.progress * 100)}%`;
        el.afLabel.textContent = update.phase ? `${update.phase} ${Math.round(update.progress * 100)}%` : "autofocus…";
        renderHud();
      },
    });
    state.afJob = finished;
    if (finished.status === "completed") {
      const best = finished.result || {};
      state.focus.position = best.position ?? state.focus.position;
      renderFocus();
      renderHud();
      toast(`focused at ${best.position} (score ${Math.round(best.score ?? 0)})`, "ok");
    } else if (finished.status === "failed") {
      toast(`autofocus failed: ${finished.error || "unknown error"}`, "error");
    }
  } catch (error) {
    reportError(error);
  } finally {
    afAbort = null;
    el.afProgress.hidden = true;
    el.af.textContent = "AF";
  }
}

el.af.addEventListener("click", () => runAutofocus(state.region));

/* -------------------------------------------------- tap the image to focus */

/**
 * The rectangle the image actually occupies inside its element.
 *
 * The <img> is letterboxed by `object-fit: contain`, so the element box is not the
 * picture: mapping a tap to a normalised region without this would be wrong whenever
 * the aspect ratios differ - which is exactly the kind of off-by-a-bit the old page
 * suffered from.
 */
function contentRect() {
  const img = el.stream;
  const rect = img.getBoundingClientRect();
  const natural = { w: img.naturalWidth, h: img.naturalHeight };
  if (!natural.w || !natural.h) return rect;
  const scale = Math.min(rect.width / natural.w, rect.height / natural.h);
  const width = natural.w * scale;
  const height = natural.h * scale;
  return {
    left: rect.left + (rect.width - width) / 2,
    top: rect.top + (rect.height - height) / 2,
    width,
    height,
    right: rect.left + (rect.width + width) / 2,
    bottom: rect.top + (rect.height + height) / 2,
  };
}

function drawMarker() {
  if (!state.region) {
    el.marker.hidden = true;
    return;
  }
  const content = contentRect();
  const stage = el.stream.parentElement.getBoundingClientRect();
  el.marker.style.left = `${content.left - stage.left + state.region.x * content.width}px`;
  el.marker.style.top = `${content.top - stage.top + state.region.y * content.height}px`;
  el.marker.style.width = `${state.region.width * content.width}px`;
  el.marker.style.height = `${state.region.height * content.height}px`;
  el.marker.hidden = false;
}

function regionFromTap(clientX, clientY, size = 0.3) {
  const content = contentRect();
  const x = Math.min(1, Math.max(0, (clientX - content.left) / content.width));
  const y = Math.min(1, Math.max(0, (clientY - content.top) / content.height));
  const half = size / 2;
  const width = Math.min(size, 1);
  const height = Math.min(size, 1);
  // Field names match models.Region exactly: this object is POSTed as-is.
  return {
    x: Math.min(1 - width, Math.max(0, x - half)),
    y: Math.min(1 - height, Math.max(0, y - half)),
    width,
    height,
  };
}

let tapStart = null;

el.stream.parentElement.addEventListener("pointerdown", (event) => {
  tapStart = { x: event.clientX, y: event.clientY, at: performance.now() };
});

el.stream.parentElement.addEventListener("pointerup", (event) => {
  const tap = tapStart;
  tapStart = null;
  if (!tap || !el.tapAf.checked) return;
  if (performance.now() - tap.at > 350) return;
  if (Math.hypot(event.clientX - tap.x, event.clientY - tap.y) > 12) return;
  if (!el.overlay.hidden) return;

  state.region = regionFromTap(event.clientX, event.clientY);
  drawMarker();
  runAutofocus(state.region);
});

window.addEventListener("resize", drawMarker);

/* ------------------------------------------------------------------ capture */

el.shutter.addEventListener("click", async () => {
  el.shutter.classList.add("is-busy");
  try {
    const photo = await api.captures.take();
    toast(`saved ${photo.filename}`, "ok");
    refreshPhotoCount();
  } catch (error) {
    reportError(error);
  } finally {
    el.shutter.classList.remove("is-busy");
  }
});

async function refreshPhotoCount() {
  try {
    const photos = await api.captures.list(200);
    state.photos = photos.length;
    el.photoCount.textContent = String(photos.length);
  } catch {
    /* counting photos is cosmetic; failing to count must never raise a toast */
  }
}

/* ------------------------------------------------------------------ gallery */

function closeDrawers() {
  // One "close everything" for the scrim, the ✕ buttons and the `g` toggle. The viewer
  // goes too: left standing while its grid is dismissed, it would be a dead end with no
  // way back to the photos.
  closeViewer();
  el.gallery.hidden = true;
  el.help.hidden = true;
  el.scrim.hidden = true;
}

function openDrawer(drawer) {
  drawer.hidden = false;
  el.scrim.hidden = false;
}

el.galleryBtn.addEventListener("click", async () => {
  openDrawer(el.gallery);
  await loadGallery();
});

el.galleryClose.addEventListener("click", closeDrawers);
el.helpBtn.addEventListener("click", () => openDrawer(el.help));
el.helpClose.addEventListener("click", closeDrawers);
el.scrim.addEventListener("click", closeDrawers);

async function loadGallery() {
  el.galleryGrid.replaceChildren();
  try {
    const photos = await api.captures.list(120);
    galleryPhotos = photos;
    state.photos = photos.length;
    el.photoCount.textContent = String(photos.length);
    photos.forEach((photo, index) => el.galleryGrid.append(thumbnail(photo, index)));
    if (!photos.length) {
      const empty = document.createElement("p");
      empty.className = "muted";
      empty.textContent = "No photos yet. Press the shutter button.";
      el.galleryGrid.append(empty);
    }
  } catch (error) {
    reportError(error);
  }
}

/** "4032×3040 · 1.2 MB · 14:32" - metadata the gallery grid has room for. */
function photoCaption(photo) {
  const size =
    photo.bytes >= 1024 * 1024
      ? `${(photo.bytes / (1024 * 1024)).toFixed(1)} MB`
      : `${Math.max(1, Math.round(photo.bytes / 1024))} kB`;
  const when = photo.created_at ? new Date(photo.created_at).toLocaleTimeString() : "";
  const dims = photo.width && photo.height ? `${photo.width}×${photo.height} · ` : "";
  return `${dims}${size}${when ? ` · ${when}` : ""}`;
}

/**
 * One tile in the gallery grid: the photo, with nothing laid over it.
 *
 * Download and delete used to be absolutely positioned on this tile. With the old 96 px
 * column minimum a phone tile carried about 117 px of picture, so two 28 px buttons and
 * their gap were most of what you could make out - the photo was the smallest thing in
 * it. Both actions moved into the viewer; tapping the tile opens it.
 */
function thumbnail(photo, index) {
  // A <button>, not a <figure>: the whole tile becomes the tap target, and a button is
  // reachable and openable from the keyboard for nothing.
  const tile = document.createElement("button");
  tile.type = "button";
  tile.className = "thumb";
  tile.title = photo.filename;

  const img = document.createElement("img");
  img.loading = "lazy";
  // Use the server-advertised URLs (so the contract stays the contract) and add the
  // token, which an <img> cannot send as a header.
  img.src = withToken(photo.thumb_url);
  img.alt = photo.filename;

  const meta = document.createElement("span");
  meta.className = "thumb-meta";
  meta.textContent = photoCaption(photo);

  tile.append(img, meta);
  tile.addEventListener("click", () => openViewer(index));
  return tile;
}

/* -------------------------------------------------------------- photo viewer */

// The listing the viewer steps through, kept next to the grid so paging through photos
// does not need the server again.
let galleryPhotos = [];
let viewerIndex = -1;

/** Show the photo at `index` of the current listing, at full size. */
function openViewer(index) {
  const photo = galleryPhotos[index];
  if (!photo) return;
  viewerIndex = index;
  el.viewer.hidden = false;
  el.viewerName.textContent = photo.filename;
  // Ask for a 1600 px rendition rather than the file: the original is a 16 MP JPEG of
  // about a megabyte, and opening one has to feel instant on a phone. "Original" and
  // "Download" both still hand over the untouched file.
  el.viewerImg.src = withToken(`${photo.url}?max_dim=1600`);
  el.viewerImg.alt = photo.filename;
  el.viewerMeta.textContent = photoCaption(photo);
  el.viewerOpen.href = withToken(photo.url);
  el.viewerDownload.href = withToken(photo.url);
  el.viewerDownload.download = photo.filename;
  // Rearm the two-step delete: paging to the next photo must not leave the button one
  // tap away from deleting it.
  el.viewerDelete.dataset.armed = "";
  el.viewerDelete.textContent = "Delete";
  el.viewerPrev.disabled = index <= 0;
  el.viewerNext.disabled = index >= galleryPhotos.length - 1;
}

function closeViewer() {
  if (el.viewer.hidden) return;
  el.viewer.hidden = true;
  // Dropping the source abandons the fetch of a photo nobody is looking at any more.
  el.viewerImg.removeAttribute("src");
  viewerIndex = -1;
}

function stepViewer(delta) {
  if (viewerIndex < 0) return;
  const next = viewerIndex + delta;
  if (next >= 0 && next < galleryPhotos.length) openViewer(next);
}

el.viewerClose.addEventListener("click", closeViewer);
el.viewerPrev.addEventListener("click", () => stepViewer(-1));
el.viewerNext.addEventListener("click", () => stepViewer(1));
el.viewer.addEventListener("click", (event) => {
  // Clicking the dark space around the picture closes it, as in every other lightbox.
  // The image and the two bars are inside .viewer, so they swallow their own clicks.
  if (event.target === el.viewer) closeViewer();
});

el.viewerDelete.addEventListener("click", async () => {
  const photo = galleryPhotos[viewerIndex];
  if (!photo) return;
  // Two-step confirm instead of window.confirm(): a blocking browser dialog steals focus
  // from the page and, on iOS, can be dismissed by an accidental tap.
  if (el.viewerDelete.dataset.armed !== "yes") {
    el.viewerDelete.dataset.armed = "yes";
    el.viewerDelete.textContent = "Sure?";
    setTimeout(() => {
      el.viewerDelete.dataset.armed = "";
      el.viewerDelete.textContent = "Delete";
    }, 3000);
    return;
  }
  try {
    await api.captures.remove(photo.filename);
    toast(`deleted ${photo.filename}`, "ok");
    // Reload from the server rather than editing the local list: the listing, the count
    // and the grid then all agree with what is on disk.
    await loadGallery();
    if (!galleryPhotos.length) closeViewer();
    else openViewer(Math.min(viewerIndex, galleryPhotos.length - 1));
  } catch (error) {
    reportError(error);
  }
});

/* ------------------------------------------------------------------ misc */

el.fullscreen.addEventListener("click", async () => {
  try {
    if (document.fullscreenElement) await document.exitFullscreen();
    else await document.documentElement.requestFullscreen();
  } catch (error) {
    reportError(error);
  }
});

document.addEventListener("keydown", (event) => {
  const target = event.target;
  const editable =
    target instanceof HTMLElement &&
    (target.matches("input, textarea, select") || target.isContentEditable);
  // Never steal keys from a field being typed in, or from the focus slider: on a
  // range input the arrows are the browser's own and give finer control than we can.
  if (editable) return;
  const onButton = target instanceof HTMLElement && target.matches("button");

  // The photo viewer owns the keyboard while it is open, and this branch has to come
  // first: below, the left/right arrows steer the camera, and looking through photos
  // must not drive the rig. Escape closes; anything else falls through to the normal
  // shortcuts, so `g`, `f` and centre still work with a photo open.
  if (!el.viewer.hidden) {
    if (event.key === "Escape") {
      event.preventDefault();
      closeViewer();
      return;
    }
    if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      event.preventDefault();
      stepViewer(event.key === "ArrowLeft" ? -1 : 1);
      return;
    }
  }

  // Arrow keys steer exactly like one full tick of the pad, so the keyboard and the
  // joystick cannot disagree about direction. The vector convention is the pad's
  // (pad.js): x is pan (-1 left, +1 right) and +y is *up on screen*, which the pad
  // paints as an upward knob travel.
  //
  // These four cases were missing while the help panel already advertised them.
  // They also work while a button has focus (holding still steers after clicking
  // "help"), which is why `button` is no longer part of the guard above - only the
  // space bar still defers to a focused button, because that is how buttons are
  // pressed from the keyboard.
  if (event.key.startsWith("Arrow")) {
    // Arrows scroll the page by default; the page is a fixed grid and must not move.
    event.preventDefault();
    const step = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, 1], ArrowDown: [0, -1] }[
      event.key
    ];
    sendNudge({ x: step[0], y: step[1] });
    return;
  }

  switch (event.key) {
    case "-":
      el.focusNear.click();
      break;
    case "+":
    case "=":
      el.focusFar.click();
      break;
    case "f":
      el.af.click();
      break;
    case " ":
      if (onButton) return; // space presses the focused button, as users expect
      event.preventDefault();
      el.shutter.click();
      break;
    case "e":
    case "E":
      // Emergency stop. One keystroke from anywhere cuts both servo pulses, which is
      // the only thing that reliably ends a rig that has started shaking: the old
      // project shipped the same shortcut for the same reason.
      el.release.click();
      break;
    case "c":
      el.center.click();
      break;
    case "g":
      el.galleryBtn.click();
      break;
    case "?":
      el.helpBtn.click();
      break;
    case "Escape":
      closeDrawers();
      break;
    default:
      break;
  }
});

/* ------------------------------------------------------------------ start */

async function boot() {
  try {
    const version = await api.version();
    document.title = `arducam-ptz ${version.version}`;
  } catch {
    /* the version is informational; the state poll reports real trouble */
  }

  try {
    const [ptz, focus] = await Promise.all([api.ptz.get(), api.focus.get()]);
    state.ptz = ptz;
    state.focus = { ...state.focus, ...focus };
    el.slider.min = String(focus.min);
    el.slider.max = String(focus.max);
    el.slider.value = String(focus.position ?? focus.min);
    const all = await api.captures.list(200);
    state.photos = all.length;
    el.photoCount.textContent = String(state.photos);
    state.focus.limits_known = true;
    renderAll();
  } catch (error) {
    reportError(error);
  }

  startStream();
  await refreshState();
  schedulePolling();
}

if (token) {
  // The interface was opened with a token: make sure it is on the stream URL too, and
  // say so, because a missing token shows up as an unhelpful broken image otherwise.
  el.streamMeta.textContent = "starting with token auth…";
}

boot();
