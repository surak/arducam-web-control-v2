#!/usr/bin/env python3
"""Drive the arducam-ptz interface in a real browser over CDP.

Why this exists: the project's own test suite can only inspect the static assets, so it
cannot answer the questions that actually matter for this UI - does the camera image
render, do the controls fit on a phone screen, do the keys do anything, does a drawer
close. Those need a real layout engine, so this script starts headless Chromium, points
it at a running server and asserts on the live DOM.

Usage:
    python3 ui_probe.py <port> <width> <height> [mobile]

Prints one JSON object with every measurement, so the caller can diff runs.
"""

from __future__ import annotations

import asyncio
import base64
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import websockets

CDP_PORT = 9333


def http_json(path: str):
    with urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}{path}", timeout=10) as fh:
        return json.load(fh)


class Browser:
    """Minimal CDP client: enough protocol to click, type and measure."""

    def __init__(self, ws_url: str) -> None:
        self.ws = None
        self.ws_url = ws_url
        self.next_id = 0
        self.events: list[dict] = []

    async def __aenter__(self):
        self.ws = await websockets.connect(self.ws_url, max_size=64 * 1024 * 1024)
        return self

    async def __aexit__(self, *exc):
        if self.ws is not None:
            await self.ws.close()

    async def send(self, method: str, **params):
        self.next_id += 1
        ident = self.next_id
        await self.ws.send(json.dumps({"id": ident, "method": method, "params": params}))
        while True:
            raw = await asyncio.wait_for(self.ws.recv(), timeout=30)
            message = json.loads(raw)
            if message.get("id") == ident:
                if "error" in message:
                    raise RuntimeError(f"{method}: {message['error']}")
                return message.get("result", {})
            if "method" in message:
                self.events.append(message)

    async def eval(self, expression: str, await_promise: bool = False):
        result = await self.send(
            "Runtime.evaluate",
            expression=expression,
            returnByValue=True,
            awaitPromise=await_promise,
        )
        if result.get("exceptionDetails"):
            return {"__error__": result["exceptionDetails"].get("text", "?")}
        return result.get("result", {}).get("value")

    async def key(self, key: str, code: str, vk: int) -> None:
        for kind in ("rawKeyDown", "keyUp"):
            await self.send(
                "Input.dispatchKeyEvent",
                type=kind,
                key=key,
                code=code,
                windowsVirtualKeyCode=vk,
                nativeVirtualKeyCode=vk,
            )

    async def click(self, selector: str) -> None:
        # json.dumps, not %r: it emits a double-quoted JS string, which is what
        # querySelector wants, and it stays correct if a selector ever needs escaping.
        selector_literal = json.dumps(selector)
        box = await self.eval(
            "(() => { const r = document.querySelector("
            + selector_literal
            + ").getBoundingClientRect();"
            " return {x: r.x + r.width / 2, y: r.y + r.height / 2}; })()"
        )
        for kind in ("mousePressed", "mouseReleased"):
            await self.send(
                "Input.dispatchMouseEvent",
                type=kind,
                x=box["x"],
                y=box["y"],
                button="left",
                clickCount=1,
            )

    async def screenshot(self, path: Path) -> None:
        shot = await self.send("Page.captureScreenshot", format="png")
        path.write_bytes(base64.b64decode(shot["data"]))


#: Everything the run needs to measure. One expression so the values are consistent.
PROBE = r"""
(() => {
  const el = (id) => document.getElementById(id);
  const style = (node) => (node ? getComputedStyle(node) : null);
  const rect = (node) => {
    if (!node) return null;
    const r = node.getBoundingClientRect();
    return {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)};
  };
  const pad = document.querySelector('.pad, #pad');
  const image = el('stream');
  const controls = Array.from(document.querySelectorAll('button, input, .pad'))
    .filter((n) => rect(n).w > 0 && rect(n).h > 0);
  const vw = innerWidth, vh = innerHeight;
  const offscreen = controls
    .filter((n) => { const r = n.getBoundingClientRect();
      return r.right > vw + 1 || r.bottom > vh + 1 || r.left < -1 || r.top < -1; })
    .map((n) => (n.id || n.className || n.tagName) + ':' + JSON.stringify(rect(n)));
  return {
    viewport: {w: vw, h: vh},
    page: {scrollW: document.documentElement.scrollWidth,
           scrollH: document.documentElement.scrollHeight},
    overlay: {hidden: el('stream-overlay').hidden,
              display: style(el('stream-overlay')).display},
    help: {hidden: el('help').hidden, display: style(el('help')).display},
    image: {naturalW: image ? image.naturalWidth : null,
            naturalH: image ? image.naturalHeight : null,
            rect: rect(image), complete: image ? image.complete : null},
    imageVisible: (() => {
      const r = image ? image.getBoundingClientRect() : null;
      if (!r) return false;
      const mid = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
      return mid ? (mid.id || mid.className || mid.tagName) : null;
    })(),
    stage: rect(el('stream') ? el('stream').parentElement : null),
    pad: rect(pad),
    padTouchAction: pad ? style(pad).touchAction : null,
    bodyUserSelect: style(document.body).userSelect,
    controlsOnScreen: controls.length,
    controlsOffscreen: offscreen,
    streamMeta: el('stream-meta') ? el('stream-meta').textContent : null,
    title: document.title
  };
})()
"""


async def main() -> int:
    port = int(sys.argv[1])
    width = int(sys.argv[2])
    height = int(sys.argv[3])
    mobile = len(sys.argv) > 4 and sys.argv[4] == "mobile"
    keep = len(sys.argv) > 5 and sys.argv[5] == "shot"

    profile = tempfile.mkdtemp(prefix="ui-probe-")
    chrome = shutil.which("chromium") or shutil.which("chromium-browser")
    proc = subprocess.Popen(
        [
            chrome,
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            "--hide-scrollbars",
            f"--remote-debugging-port={CDP_PORT}",
            f"--user-data-dir={profile}",
            f"--window-size={width},{height}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    out: dict = {"viewport": f"{width}x{height}", "mobile": mobile}
    try:
        # Wait for the debugging endpoint instead of sleeping blindly.
        for _ in range(60):
            try:
                targets = http_json("/json/list")
                if targets:
                    break
            except Exception:
                time.sleep(0.5)
        page = next(t for t in targets if t["type"] == "page")
        async with Browser(page["webSocketDebuggerUrl"]) as browser:
            await browser.send("Runtime.enable")
            await browser.send("Log.enable")
            await browser.send("Page.enable")
            await browser.send("Emulation.setDeviceMetricsOverride",
                               width=width, height=height, deviceScaleFactor=2 if mobile else 1,
                               mobile=mobile)
            await browser.send("Page.navigate", url=f"http://127.0.0.1:{port}/")
            await asyncio.sleep(6)  # boot + a few MJPEG frames

            out["probe"] = await browser.eval(PROBE)

            # --- help drawer must open *and close* (the [hidden] bug)
            await browser.click("#btn-help")
            await asyncio.sleep(0.4)
            out["help_after_open"] = await browser.eval(
                "({hidden: document.getElementById('help').hidden,"
                " display: getComputedStyle(document.getElementById('help')).display})"
            )
            await browser.click("#btn-help-close")
            await asyncio.sleep(0.4)
            out["help_after_close"] = await browser.eval(
                "({hidden: document.getElementById('help').hidden,"
                " display: getComputedStyle(document.getElementById('help')).display})"
            )

            # --- arrow keys must reach the API
            state_before = await browser.eval(
                "fetch('/api/v1/state').then(r => r.json()).then(s => s.ptz.pan.angle)",
                await_promise=True,
            )
            await browser.key("ArrowRight", "ArrowRight", 39)
            await asyncio.sleep(1.2)
            state_after = await browser.eval(
                "fetch('/api/v1/state').then(r => r.json()).then(s => s.ptz.pan.angle)",
                await_promise=True,
            )
            tilt_before = await browser.eval(
                "fetch('/api/v1/state').then(r => r.json()).then(s => s.ptz.tilt.angle)",
                await_promise=True,
            )
            await browser.key("ArrowUp", "ArrowUp", 38)
            await asyncio.sleep(1.2)
            tilt_after = await browser.eval(
                "fetch('/api/v1/state').then(r => r.json()).then(s => s.ptz.tilt.angle)",
                await_promise=True,
            )
            out["arrow_right"] = {"pan_before": state_before, "pan_after": state_after}
            out["arrow_up"] = {"tilt_before": tilt_before, "tilt_after": tilt_after}

            # --- emergency stop key must release the axes
            await browser.key("e", "KeyE", 69)
            await asyncio.sleep(1.0)
            out["after_E"] = await browser.eval(
                "fetch('/api/v1/state').then(r => r.json()).then(s =>"
                " ({released: s.ptz.pan.released, detail: s.ptz.detail}))",
                await_promise=True,
            )

            # --- tap the image to autofocus.
            # This is the path that shipped broken: the UI sent {x, y, w, h} while the
            # API's Region wants width/height, so every tap produced a 422 that the page
            # showed as raw JSON. The probe taps for real and then asks the server
            # whether a sweep actually ran, so field-name drift fails the run instead of
            # only being visible to whoever happened to click.
            if not await browser.eval("document.getElementById('tap-af').checked"):
                await browser.click("#tap-af")
            tap_at = await browser.eval(
                "(() => { const r = document.getElementById('stream').getBoundingClientRect();"
                " return {x: r.x + r.width / 2, y: r.y + r.height / 2}; })()"
            )
            for kind in ("mousePressed", "mouseReleased"):
                await browser.send(
                    "Input.dispatchMouseEvent",
                    type=kind,
                    x=tap_at["x"],
                    y=tap_at["y"],
                    button="left",
                    clickCount=1,
                )
            await asyncio.sleep(3.0)
            out["tap_to_focus"] = await browser.eval(
                "(() => { const t = document.querySelector('#toasts .toast');"
                " return {toast: t ? t.textContent : null,"
                " toastKind: t ? t.className : null,"
                " markerShown: !document.getElementById('af-marker').hidden}; })()"
            )
            out["tap_to_focus_job"] = await browser.eval(
                "fetch('/api/v1/state').then(r => r.json()).then(s => s.focus.autofocus)",
                await_promise=True,
            )

            # --- the photo viewer.
            # The gallery used to lay download/delete over a ~117 px tile, so the two
            # buttons were most of what the tile showed. Tiles now carry no buttons and
            # tapping one opens the photo large. This measures the rendered result
            # instead of trusting the stylesheet.
            await browser.eval("document.getElementById('btn-gallery').click()")
            await asyncio.sleep(2.5)
            out["gallery"] = await browser.eval(
                "(() => { const tiles = [...document.querySelectorAll('.thumb')];"
                " const img = document.querySelector('.thumb img');"
                " const r = img ? img.getBoundingClientRect() : null;"
                " return {tiles: tiles.length,"
                " imageWidth: r ? Math.round(r.width) : null,"
                " imageHeight: r ? Math.round(r.height) : null,"
                " thumbLoaded: img ? img.naturalWidth : null,"
                " buttonsOnTiles: document.querySelectorAll('.thumb-actions').length}; })()"
            )

            await browser.eval("document.querySelector('.thumb').click()")
            await asyncio.sleep(2.5)
            out["viewer"] = await browser.eval(
                "(() => { const v = document.getElementById('viewer');"
                " const img = document.getElementById('viewer-img');"
                " const r = img.getBoundingClientRect();"
                " const actions = document.querySelectorAll('.viewer-actions .btn');"
                " return {hidden: v.hidden, display: getComputedStyle(v).display,"
                " imageWidth: Math.round(r.width), imageHeight: Math.round(r.height),"
                " imageLoaded: img.naturalWidth,"
                " heightShareOfViewport: Math.round((r.height / innerHeight) * 100),"
                " actions: actions.length,"
                " name: document.getElementById('viewer-name').textContent}; })()"
            )

            # Arrows must browse photos while the viewer is open, and must not steer the
            # camera: the steering branch sits below this one, and getting that order
            # wrong would move the rig while you look through pictures.
            pan_before_arrows = await browser.eval(
                "fetch('/api/v1/state').then(r => r.json()).then(s => s.ptz.pan.angle)",
                await_promise=True,
            )
            name_before = await browser.eval("document.getElementById('viewer-name').textContent")
            await browser.key("ArrowRight", "ArrowRight", 39)
            await asyncio.sleep(1.2)
            out["viewer_arrows"] = {
                "name_before": name_before,
                "name_after": await browser.eval(
                    "document.getElementById('viewer-name').textContent"
                ),
                "pan_before": pan_before_arrows,
                "pan_after": await browser.eval(
                    "fetch('/api/v1/state').then(r => r.json()).then(s => s.ptz.pan.angle)",
                    await_promise=True,
                ),
            }

            await browser.key("Escape", "Escape", 27)
            await asyncio.sleep(0.5)
            out["viewer_after_escape"] = await browser.eval(
                "({hidden: document.getElementById('viewer').hidden,"
                " src: document.getElementById('viewer-img').getAttribute('src')})"
            )

            if keep:
                # Name it after the viewport so a mobile and a desktop run can coexist.
                shot = Path(f"ui_shot_{width}x{height}.png").resolve()
                await browser.screenshot(shot)
                out["screenshot"] = str(shot)

            errors = [
                e["params"]["entry"]["text"]
                for e in browser.events
                if e["method"] == "Log.entryAdded"
                and e["params"]["entry"]["level"] in ("error", "warning")
            ]
            out["console"] = errors[:10]
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)

    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
