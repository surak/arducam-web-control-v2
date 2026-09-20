/*
 * The pan/tilt pad.
 *
 * Model: a *velocity* stick, not a position field. Holding the knob away from the
 * centre keeps sending the same nudge until you let go, which is how a pan-tilt head
 * should feel - you are steering, not teleporting. A quick tap without moving sends a
 * single small step in that direction, because sometimes you just want a little nudge.
 *
 * Why it is written this way:
 *
 * * Pointer Events (one code path for mouse, touch and pen), with setPointerCapture so
 *   a drag that leaves the pad keeps steering instead of getting lost.
 * * `touch-action: none` lives in the CSS on `.pad`; that is what stops iOS from
 *   zooming the text or selecting it while you drag - the failure the old page had.
 * * Movement is emitted at a fixed rate (default 15 Hz) regardless of how many pointer
 *   events arrive. Browsers fire pointermove at up to 1000 Hz on a trackpad; sending
 *   all of that would flood the server and the serial bus. The hub coalesces writes
 *   anyway, but being polite is free.
 * * The knob is drawn with a transform on an absolutely positioned element, so a drag
 *   never triggers layout or reflow.
 */

const DEADZONE = 0.18;

export class Pad {
  /**
   * @param {HTMLElement} el      the pad surface
   * @param {object} options
   * @param {HTMLElement} options.knob      the element that follows the finger
   * @param {(vector: {x: number, y: number}) => void} options.onVector
   *        called at ~rateHz while the pad is held; x is pan (-1 left, +1 right),
   *        y is tilt (-1 up (down in screen terms) ... see the note below)
   * @param {(vector: {x: number, y: number}) => void} options.onTap
   *        called for a short press that did not move: one small step
   * @param {number} [options.rateHz]
   */
  constructor(el, { knob, onVector, onTap, rateHz = 15 }) {
    this.el = el;
    this.knob = knob;
    this.onVector = onVector;
    this.onTap = onTap;
    this.periodMs = 1000 / rateHz;

    this.vector = { x: 0, y: 0 };
    this.pointerId = null;
    this.keys = new Set();
    this.timer = null;
    this.pressStart = null;
    this.moved = 0;

    this._onPointerDown = this._onPointerDown.bind(this);
    this._onPointerMove = this._onPointerMove.bind(this);
    this._onPointerUp = this._onPointerUp.bind(this);
    this._onKeyDown = this._onKeyDown.bind(this);
    this._onKeyUp = this._onKeyUp.bind(this);

    el.addEventListener("pointerdown", this._onPointerDown);
    el.addEventListener("pointermove", this._onPointerMove);
    el.addEventListener("pointerup", this._onPointerUp);
    el.addEventListener("pointercancel", this._onPointerUp);
    el.addEventListener("keydown", this._onKeyDown);
    el.addEventListener("keyup", this._onKeyUp);
    el.addEventListener("blur", () => this._stop());
  }

  /** True while the user is holding the pad: the UI uses it to stop polling. */
  get isActive() {
    return this.pointerId !== null || this.keys.size > 0;
  }

  destroy() {
    this._stop();
    this.el.removeEventListener("pointerdown", this._onPointerDown);
    this.el.removeEventListener("pointermove", this._onPointerMove);
    this.el.removeEventListener("pointerup", this._onPointerUp);
    this.el.removeEventListener("pointercancel", this._onPointerUp);
    this.el.removeEventListener("keydown", this._onKeyDown);
    this.el.removeEventListener("keyup", this._onKeyUp);
  }

  _vectorFromEvent(event) {
    const rect = this.el.getBoundingClientRect();
    const radius = Math.min(rect.width, rect.height) / 2;
    const dx = (event.clientX - (rect.left + rect.width / 2)) / radius;
    // Screen y grows downwards; the caller wants +y = up, so it is flipped here once.
    const dy = -(event.clientY - (rect.top + rect.height / 2)) / radius;
    return this._normalise(dx, dy);
  }

  _normalise(x, y) {
    const magnitude = Math.hypot(x, y);
    if (magnitude <= DEADZONE) return { x: 0, y: 0 };
    if (magnitude > 1) {
      x /= magnitude;
      y /= magnitude;
    }
    return { x, y };
  }

  _paint() {
    if (!this.knob) return;
    // The knob travels inside the pad; 0.29 of the pad width is a comfortable throw.
    const rect = this.el.getBoundingClientRect();
    const travel = Math.min(rect.width, rect.height) * 0.29;
    this.knob.style.transform = `translate3d(${this.vector.x * travel}px, ${-this.vector.y * travel}px, 0)`;
  }

  _emit() {
    this.onVector({ ...this.vector });
  }

  _start() {
    if (this.timer !== null) return;
    this.el.classList.add("is-dragging");
    this._emit(); // immediate feedback, then at a steady rate
    this.timer = window.setInterval(() => this._emit(), this.periodMs);
  }

  _stop() {
    if (this.timer !== null) {
      window.clearInterval(this.timer);
      this.timer = null;
    }
    this.el.classList.remove("is-dragging");
    this.vector = { x: 0, y: 0 };
    this.keys.clear();
    this._paint();
  }

  _onPointerDown(event) {
    if (this.pointerId !== null) return;
    // Keeps the drag alive when the finger slides off the pad, and prevents the
    // browser from turning the gesture into a scroll or a text selection.
    this.el.setPointerCapture(event.pointerId);
    this.pointerId = event.pointerId;
    this.pressStart = { x: event.clientX, y: event.clientY, at: performance.now() };
    this.moved = 0;
    this.vector = this._vectorFromEvent(event);
    this._paint();
    this._start();
    event.preventDefault();
  }

  _onPointerMove(event) {
    if (event.pointerId !== this.pointerId) return;
    this.vector = this._vectorFromEvent(event);
    this._paint();
    if (this.pressStart) {
      this.moved = Math.max(
        this.moved,
        Math.hypot(event.clientX - this.pressStart.x, event.clientY - this.pressStart.y),
      );
    }
    event.preventDefault();
  }

  _onPointerUp(event) {
    if (event.pointerId !== this.pointerId) return;
    const start = this.pressStart;
    const vector = this._vectorFromEvent(event);
    this.pointerId = null;
    this.pressStart = null;
    this._stop();

    // A short press that barely moved = "nudge that way", even if it landed inside the
    // dead zone (a tap in the middle should do nothing, which the zero vector handles).
    const isTap = start && performance.now() - start.at < 260 && this.moved < 12;
    if (isTap && this.onTap) {
      const tapVector = vector.x === 0 && vector.y === 0 ? { x: 0, y: 0 } : vector;
      if (tapVector.x !== 0 || tapVector.y !== 0) this.onTap(tapVector);
    }
  }

  _onKeyDown(event) {
    const key = event.key;
    if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(key)) return;
    event.preventDefault(); // otherwise the arrows scroll the page
    this.keys.add(key);
    this.vector = this._vectorFromKeys();
    this._paint();
    this._start();
  }

  _onKeyUp(event) {
    if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) return;
    this.keys.delete(event.key);
    if (this.keys.size === 0) {
      this._stop();
    } else {
      this.vector = this._vectorFromKeys();
      this._paint();
    }
  }

  _vectorFromKeys() {
    let x = 0;
    let y = 0;
    if (this.keys.has("ArrowLeft")) x -= 1;
    if (this.keys.has("ArrowRight")) x += 1;
    if (this.keys.has("ArrowUp")) y += 1;
    if (this.keys.has("ArrowDown")) y -= 1;
    return this._normalise(x, y);
  }
}
