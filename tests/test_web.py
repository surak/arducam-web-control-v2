"""Static-interface tests.

There is no browser in CI, so these check the front-end the way a linter would: they
serve the assets through the real app and inspect the source for the specific mistakes
the old page made. Each guard is a regression test for a bug the user actually reported:

* controls pushed off screen when the image is visible  -> the fixed app shell
* touching the pad zooming the page / selecting text    -> touch-action + user-select
* a frozen stream that looks live                       -> the stream watchdog exists
* re-rendering innerHTML and losing state mid-drag      -> no innerHTML at all
* element ids that do not exist (silent no-ops in JS)   -> ids cross-checked

They are cheap, they run everywhere, and they fail loudly when someone "fixes" the
layout by reintroducing a scrolling column.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from arducam_ptz.api.app import WEB_DIR, create_app
from arducam_ptz.services.context import AppContext

INDEX = WEB_DIR / "index.html"
CSS = WEB_DIR / "app.css"
APP_JS = WEB_DIR / "js" / "app.js"
API_JS = WEB_DIR / "js" / "api.js"
PAD_JS = WEB_DIR / "js" / "pad.js"


@pytest.fixture
def client(settings) -> Iterator[TestClient]:
    context = AppContext.build(settings)
    app = create_app(settings, context=context)
    context.start()
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        context.close()


class TestAssetsAreServed:
    def test_root_serves_the_interface(self, client: TestClient) -> None:
        response = client.get("/")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert "arducam-ptz" in response.text

    @pytest.mark.parametrize(
        ("path", "prefix"),
        [
            ("app.css", "text/css"),
            ("js/app.js", "text/javascript"),
            ("js/api.js", "text/javascript"),
            ("js/pad.js", "text/javascript"),
            ("icon.svg", "image/svg+xml"),
            ("manifest.webmanifest", "application/manifest+json"),
            ("apple-touch-icon.png", "image/png"),
        ],
    )
    def test_each_asset_is_served_with_the_right_type(self, client: TestClient, path: str, prefix: str) -> None:
        response = client.get(f"/{path}")
        assert response.status_code == 200, path
        assert response.headers["content-type"].startswith(prefix), path

    def test_the_api_is_not_shadowed_by_the_static_mount(self, client: TestClient) -> None:
        """The UI is mounted at "/", so every API route must still win."""
        assert client.get("/api/v1/health").status_code == 200
        assert client.get("/api/version").json()["name"] == "arducam-ptz"
        assert client.get("/openapi.json").status_code == 200


class TestNoGoldenOldies:
    """Regressions of the specific problems in the original interface."""

    def test_viewport_does_not_disable_zoom(self) -> None:
        """Blocks accidental zoom the right way.

        The lazy fix for "the page zooms when I drag" is `maximum-scale=1, user-scalable=no`,
        which breaks pinch-zoom for everyone. Zoom is instead prevented at the source,
        with `touch-action: none` on the surfaces that are draggable.
        """
        html = INDEX.read_text()
        viewport = re.search(r'<meta name="viewport" content="([^"]+)"', html)
        assert viewport, "no viewport meta tag"
        content = viewport.group(1).lower()
        assert "maximum-scale" not in content
        assert "user-scalable=no" not in content.replace(" ", "")

    def test_draggable_surfaces_opt_out_of_browser_gestures(self) -> None:
        css = CSS.read_text()
        pad_block = re.search(r"\.pad \{(.*?)\}", css, re.DOTALL)
        assert pad_block, ".pad rule not found in app.css"
        assert "touch-action: none" in pad_block.group(1)

    def test_controls_are_not_selectable_or_highlighted(self) -> None:
        css = CSS.read_text()
        assert "user-select: none" in css
        assert "-webkit-tap-highlight-color" in css

    def test_the_page_itself_never_scrolls(self) -> None:
        """A fixed shell is what keeps the image and the controls on screen together."""
        css = CSS.read_text()
        html_block = re.search(r"html,\s*body \{(.*?)\}", css, re.DOTALL)
        assert html_block, "html,body rule not found"
        assert "overflow: hidden" in html_block.group(1)
        assert "overscroll-behavior: none" in html_block.group(1)
        assert "100dvh" in css  # dynamic viewport height: mobile browser bars accounted for

    def test_the_image_is_letterboxed_not_cropped(self) -> None:
        css = CSS.read_text()
        assert "object-fit: contain" in css
        assert "min-height: 0" in css  # lets the image shrink instead of hiding controls

    def test_javascript_never_rebuilds_the_dom(self) -> None:
        """innerHTML on every poll is what made the old page drop selections and reflow."""
        for path in (APP_JS, API_JS, PAD_JS):
            source = path.read_text()
            # Assignments only: the files *talk* about innerHTML in comments explaining
            # why it is not used, and those comments are the documentation.
            assert not re.search(r"\.innerHTML\s*=", source), f"{path.name} assigns innerHTML"
            assert "document.write" not in source, f"{path.name} uses document.write"

    def test_a_frozen_stream_is_detected(self) -> None:
        """The old page showed a stale frame forever and looked alive."""
        source = APP_JS.read_text()
        assert "last_frame_age_s" in source
        assert "stream stalled" in source

    def test_the_pad_uses_pointer_events_and_captures_them(self) -> None:
        source = PAD_JS.read_text()
        assert "pointerdown" in source and "pointermove" in source
        assert "setPointerCapture" in source, "a drag that leaves the pad would be lost"
        assert "touch-action" in CSS.read_text()

    def test_tap_to_focus_maps_the_visible_image_not_the_element(self) -> None:
        """object-fit letterboxing means the element box is not the picture."""
        source = APP_JS.read_text()
        assert "contentRect" in source
        assert "naturalWidth" in source and "naturalHeight" in source

    def test_the_autofocus_flow_polls_a_job(self) -> None:
        assert "pollJob" in APP_JS.read_text()
        assert "pollJob" in API_JS.read_text()


class TestInterfaceContract:
    """Cross-checks between the JavaScript, the HTML and the API."""

    def test_every_element_id_used_by_js_exists_in_the_html(self) -> None:
        html_ids = set(re.findall(r'id="([^"]+)"', INDEX.read_text()))
        used = set(re.findall(r'\$\("([^"]+)"\)', APP_JS.read_text()))
        missing = sorted(used - html_ids)
        assert not missing, f"app.js looks for ids that do not exist: {missing}"

    def test_every_html_id_is_actually_used(self) -> None:
        """Dead ids are how a control silently stops working after a refactor."""
        html_ids = set(re.findall(r'id="([^"]+)"', INDEX.read_text()))
        source = APP_JS.read_text() + API_JS.read_text()
        used = set(re.findall(r'\$\("([^"]+)"\)', source))
        # `$` is only used for el.* lookups in app.js; other ids are referenced by name.
        unused = sorted(identifier for identifier in html_ids - used if f'"{identifier}"' not in source)
        assert not unused, f"ids in index.html that nothing references: {unused}"

    def test_api_paths_used_by_the_ui_exist_in_the_app(self, client: TestClient) -> None:
        """Every endpoint the UI calls must be routed.

        The paths are scraped out of js/api.js and fetched, so a typo or a renamed route
        fails here instead of surfacing as a mysterious 404 in the browser.
        """
        source = API_JS.read_text()
        paths = set(re.findall(r"`\$\{API\}(/[a-z0-9/.\-${}]*?)`", source))
        assert paths, "no API paths found in js/api.js"
        openapi = client.get("/openapi.json").json()["paths"]
        templates = {
            "/api/v1" + re.sub(r"\$\{[^}]*\}", "{x}", path).split("?")[0].rstrip("/") for path in paths
        }
        for template in sorted(templates):
            concrete = template.replace("{x}", "abc")
            matches = [
                route
                for route in openapi
                if re.fullmatch(re.sub(r"\{[^}]+\}", r"[^/]+", route), concrete)
            ]
            assert matches, f"the UI calls {template} but no route matches it"
