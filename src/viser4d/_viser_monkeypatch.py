from __future__ import annotations

from pathlib import Path

import viser
from viser import _client_autobuild

PATCH_DIR = Path(__file__).resolve().parent / "client" / "viser-monkeypatch"
CLIENT_SRC = Path(viser.__file__).resolve().parent / "client" / "src"


def _snippet(name: str) -> str:
    return (PATCH_DIR / name).read_text()


def _insert_once(text: str, marker: str, anchor: str, snippet: str) -> str:
    if marker in text:
        return text
    assert anchor in text
    return text.replace(anchor, snippet + "\n" + anchor, 1)


def _replace(text: str, before: str, after: str) -> str:
    if after in text:
        return text
    assert before in text
    return text.replace(before, after)

FILE_PLAYBACK_REPLACEMENTS = (
    (
        "  function resetScene() {\n",
        "  function resetScene() {\n"
        "    ensureViser4dFileAudioRuntime().resetAll();\n",
    ),
    (
        "      const message = recording.messages[mutable.currentIndex][1];\n",
        "      const message = recording.messages[mutable.currentIndex][1];\n"
        "      if (isViser4dAudioMessage(message)) {\n"
        "        (message as Message & { __viserPlaybackTime?: number }).__viserPlaybackTime =\n"
        "          recording.messages[mutable.currentIndex][0];\n"
        "      }\n",
    ),
    (
        "    setCurrentTime(mutable.currentTime);\n",
        "    setCurrentTime(mutable.currentTime);\n"
        "    ensureViser4dFileAudioRuntime().seek(mutable.currentTime, !paused);\n",
    ),
)


def ensure_viser_audio_patch() -> None:
    message_handler_path = CLIENT_SRC / "MessageHandler.tsx"
    file_playback_path = CLIENT_SRC / "FilePlayback.tsx"
    original_message_handler = message_handler_path.read_text()
    original_file_playback = file_playback_path.read_text()

    message_handler = _insert_once(
        original_message_handler,
        "// __VISER4D_AUDIO_MESSAGES__",
        "/** Returns a handler for all incoming messages. */",
        _snippet("message-handler.ts"),
    )
    file_playback = _insert_once(
        original_file_playback,
        "// __VISER4D_AUDIO_PLAYBACK__",
        "export interface SerializedMessages {\n",
        _snippet("file-playback.ts"),
    )
    for before, after in FILE_PLAYBACK_REPLACEMENTS:
        file_playback = _replace(file_playback, before, after)

    if message_handler == original_message_handler and file_playback == original_file_playback:
        return

    message_handler_path.write_text(message_handler)
    file_playback_path.write_text(file_playback)
    _client_autobuild._build_viser_client(_client_autobuild.build_dir, cached=False)
