from __future__ import annotations

import os
import subprocess
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

import numpy as np
import pytest
import viser4d


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        del format, args


@pytest.mark.e2e
def test_exported_viser_embed_has_no_console_errors(tmp_path: Path) -> None:
    if os.getenv("VISER4D_RUN_E2E") != "1":
        pytest.skip("Set VISER4D_RUN_E2E=1 to run browser E2E tests.")

    playwright = pytest.importorskip("playwright.sync_api")

    recordings_dir = tmp_path / "recordings"
    client_dir = tmp_path / "viser-client"
    recordings_dir.mkdir(parents=True)

    server = viser4d.Viser4dServer(
        num_steps=5,
        host="127.0.0.1",
        port=0,
        verbose=False,
        enable_playback_gui=False,
    )
    try:
        with server.at(0):
            handle = server.scene.add_frame("/frame", axes_length=0.1)
            handle.position = (0.0, 0.0, 0.0)
            server.scene.add_audio(
                "/tone",
                data=np.sin(np.linspace(0.0, 8.0, 1600)).astype(np.float32),
                sample_rate=16000,
            )
        with server.at(1):
            handle.position = (1.0, 0.0, 0.0)
        with server.at(2):
            handle.position = (2.0, 0.0, 0.0)
        with server.at(3):
            handle.position = (3.0, 0.0, 0.0)
        with server.at(4):
            handle.position = (4.0, 0.0, 0.0)

        server.serialize(recordings_dir / "recording.viser")
    finally:
        server.stop()

    subprocess.run(
        ["viser-build-client", "--out-dir", str(client_dir)],
        check=True,
        capture_output=True,
        text=True,
    )

    httpd = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        partial(_QuietHandler, directory=str(tmp_path)),
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    port = httpd.server_port
    playback_url = f"http://127.0.0.1:{port}/recordings/recording.viser"
    viewer_url = (
        f"http://127.0.0.1:{port}/viser-client/"
        f"?playbackPath={quote(playback_url, safe='')}"
    )

    console_errors: list[str] = []
    page_errors: list[str] = []
    try:
        with playwright.sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
            page = browser.new_page()
            page.on(
                "console",
                lambda msg: (
                    console_errors.append(msg.text) if msg.type == "error" else None
                ),
            )
            page.on("pageerror", lambda err: page_errors.append(str(err)))

            page.goto(viewer_url, wait_until="networkidle", timeout=60000)
            page.wait_for_function(
                "() => typeof window.__viser4d_audio !== 'undefined'",
                timeout=15000,
            )
            page.wait_for_timeout(3000)
            browser.close()
    finally:
        httpd.shutdown()
        httpd.server_close()

    filtered_console_errors = [
        err for err in console_errors if "favicon.ico" not in err.lower()
    ]
    assert page_errors == []
    assert filtered_console_errors == []
