from __future__ import annotations

import contextlib
import socket
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from typing import Any, cast

from playwright.sync_api import Browser, Page

import viser4d

_TIMEOUT_S = 10.0


def _pick_unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return cast(int, sock.getsockname()[1])


def _wait_until(
    predicate: Callable[[], bool],
    *,
    timeout: float = _TIMEOUT_S,
    interval: float = 0.05,
    message: str,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError(f"Timed out waiting for {message}.")


def _server_is_ready(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=1.0) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


@contextlib.contextmanager
def _live_server(
    *,
    num_steps: int,
    fps: float = 30.0,
    loop: bool = False,
    playback_speed: float = 1.0,
) -> Iterator[tuple[viser4d.Viser4dServer, str]]:
    port = _pick_unused_port()
    server = viser4d.Viser4dServer(
        num_steps=num_steps,
        fps=fps,
        loop=loop,
        playback_speed=playback_speed,
        host="127.0.0.1",
        port=port,
        verbose=False,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        _wait_until(
            lambda: _server_is_ready(url),
            message=f"server {url}",
        )
        yield server, url
    finally:
        server.stop()


def _open_viewer(page: Page, url: str) -> None:
    page.goto(url, wait_until="load")
    page.wait_for_function("() => window.__VISER4D__ !== undefined")
    _wait_for_debug_event(page, "runtime.configure")


def _clear_debug_logs(page: Page) -> None:
    page.evaluate("() => window.__VISER4D__.debug.clear()")


def _debug_logs(page: Page) -> list[dict[str, Any]]:
    return cast(
        list[dict[str, Any]], page.evaluate("() => window.__VISER4D__.debug.logs")
    )


def _latest_debug_payload(page: Page, event: str) -> dict[str, Any]:
    matching = [log for log in _debug_logs(page) if log.get("event") == event]
    assert matching
    return cast(dict[str, Any], matching[-1]["payload"])


def _wait_for_debug_event(
    page: Page,
    event: str,
    *,
    timeout: float = _TIMEOUT_S,
    **criteria: object,
) -> None:
    page.wait_for_function(
        """
        ([event, criteria]) => {
          const runtime = window.__VISER4D__;
          if (!runtime) {
            return false;
          }
          return runtime.debug.logs.some((log) => {
            if (log.event !== event) {
              return false;
            }
            const payload = log.payload ?? {};
            return Object.entries(criteria).every(
              ([key, value]) => payload?.[key] === value,
            );
          });
        }
        """,
        arg=[event, criteria],
        timeout=int(timeout * 1000),
    )


def _slider_value(page: Page, label: str) -> float:
    value = (
        page.get_by_text(label, exact=True)
        .locator("xpath=following::div[@role='slider'][1]")
        .get_attribute("aria-valuenow")
    )
    assert value is not None
    return float(value)


def test_browser_playback_config_and_controls_round_trip(
    browser: Browser,
    page: Page,
) -> None:
    with _live_server(num_steps=3, loop=True, playback_speed=1.5) as (server, url):
        steps: list[int] = []
        playback_states: list[bool] = []
        server.on_timestep_change(lambda _client, step: steps.append(step))
        server.on_playback_change(
            lambda _client, is_playing: playback_states.append(is_playing)
        )

        with server.at(0) as timeline:
            joint = timeline.scene.add_frame("/joint")
        with server.at(1):
            joint.position = (1.0, 0.0, 0.0)

        _open_viewer(page, url)

        config = _latest_debug_payload(page, "runtime.configure")
        assert config["numSteps"] == 3
        assert config["speed"] == 1.5
        assert config["loop"] is True
        assert _slider_value(page, "Speed") == 1.5

        server.set_playback_speed(0.5)
        server.set_loop(False)

        _wait_for_debug_event(page, "runtime.configure", speed=0.5, loop=False)
        _wait_until(
            lambda: _slider_value(page, "Speed") == 0.5,
            message="updated speed slider",
        )

        page.get_by_role("button", name="Next").click()
        _wait_until(lambda: steps[-1:] == [1], message="timestep callback")

        page.get_by_role("button", name="Play").click()
        _wait_until(
            lambda: playback_states[-1:] == [True],
            message="playback start callback",
        )

        pause_button = page.get_by_role("button", name="Pause")
        pause_button.wait_for(state="visible")
        pause_button.click()
        _wait_until(
            lambda: playback_states[-1:] == [False],
            message="playback pause callback",
        )

        other_context = browser.new_context(viewport={"width": 1400, "height": 1000})
        try:
            other_page = other_context.new_page()
            other_page.set_default_timeout(10_000)
            _open_viewer(other_page, url)
            other_config = _latest_debug_payload(other_page, "runtime.configure")
            assert other_config["speed"] == 0.5
            assert other_config["loop"] is False
        finally:
            other_context.close()


def test_browser_replays_existing_global_overrides_on_connect(page: Page) -> None:
    with _live_server(num_steps=2) as (server, url):
        page_errors: list[str] = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))

        with server.at(0) as timeline:
            joint = timeline.scene.add_frame("/joint")

        joint.position = (2.0, 0.0, 0.0)

        _open_viewer(page, url)
        _wait_for_debug_event(
            page,
            "runtime.apply_message_update",
            key="scene:SetPositionMessage:/joint",
            type="SetPositionMessage",
            name="/joint",
        )
        assert page_errors == []


def test_browser_applies_live_scene_removals_without_block_reload(page: Page) -> None:
    with _live_server(num_steps=2) as (server, url):
        page_errors: list[str] = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))

        with server.at(0) as timeline:
            joint = timeline.scene.add_frame("/joint")

        _open_viewer(page, url)
        page.wait_for_timeout(200)
        _clear_debug_logs(page)

        joint.remove()

        _wait_for_debug_event(
            page,
            "runtime.apply_message_update",
            key="scene:delete:/joint",
            type="RemoveSceneNodeMessage",
            name="/joint",
        )
        logs = _debug_logs(page)
        assert not any(
            log.get("event") in {"runtime.load_block", "runtime.patch_block"}
            for log in logs
        )
        assert page_errors == []
