from __future__ import annotations

from viser4d._generate_runtime_message_ts import (
    generate_runtime_messages_typescript,
    generated_runtime_messages_path,
)


def test_generated_runtime_message_typescript_is_up_to_date() -> None:
    assert (
        generated_runtime_messages_path().read_text()
        == generate_runtime_messages_typescript()
    )
