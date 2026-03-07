from __future__ import annotations

import socket
import subprocess
import sys
import textwrap
import time
from contextlib import closing

import pytest
from playwright.sync_api import sync_playwright


def _free_port() -> int:
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.e2e
def test_runtime_replays_recorded_steps_in_browser() -> None:
    port = _free_port()
    script = textwrap.dedent(
        f"""
        import numpy as np
        import viser4d

        server = viser4d.Viser4dServer(num_steps=3, port={port}, verbose=False)
        server.scene.add_frame('/static', axes_length=0.2)
        with server.at(0):
            frame = server.scene.add_frame('/moving', axes_length=0.15)
            audio = server.scene.add_audio(
                '/audio',
                data=np.array([0, 1000, -1000, 0], dtype=np.int16),
                sample_rate=8000,
            )
        with server.at(1):
            frame.position = (1.0, 0.0, 0.0)
            audio.volume = 0.5
        with server.at(2):
            frame.position = (2.0, 0.0, 0.0)
        server.sleep_forever()
        """
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(1.5)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(
                f"http://127.0.0.1:{port}", wait_until="networkidle", timeout=30_000
            )
            page.wait_for_timeout(2_000)

            initial = page.evaluate(
                """() => {
                    const viewer = window.__VISER4D__.getViewer();
                    const state = viewer.useSceneTree.getState();
                    return {
                      hasViewer: !!viewer,
                      stepSizes: window.__VISER4D__.sceneSteps.map((x) => x.length),
                      movingExists: !!state['/moving'],
                    };
                }"""
            )
            assert initial == {
                "hasViewer": True,
                "stepSizes": [2, 1, 1],
                "movingExists": False,
            }

            page.evaluate("window.__VISER4D__.seek({step: 1})")
            page.wait_for_timeout(300)
            seek_state = page.evaluate(
                """() => {
                    const state = window.__VISER4D__.getViewer().useSceneTree.getState();
                    return state['/moving']?.position ?? null;
                }"""
            )
            assert seek_state == [1, 0, 0]

            page.evaluate("window.__VISER4D__.play({fps: 8, loop: false})")
            page.wait_for_timeout(900)
            play_state = page.evaluate(
                """() => {
                    const state = window.__VISER4D__.getViewer().useSceneTree.getState();
                    return {
                      position: state['/moving']?.position ?? null,
                      appliedStep: window.__VISER4D__.appliedStep,
                    };
                }"""
            )
            assert play_state == {"position": [2, 0, 0], "appliedStep": 2}
            browser.close()
    finally:
        proc.terminate()
        proc.wait(timeout=5)
