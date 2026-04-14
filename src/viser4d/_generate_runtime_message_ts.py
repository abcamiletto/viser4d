from pathlib import Path

from ._runtime_messages import RuntimeSceneMessage, _RuntimeMessageBase
from ._typescript_interface_gen import generate_typescript_interfaces


def generated_runtime_messages_path() -> Path:
    return (
        Path(__file__).resolve().parent
        / "client"
        / "bridge"
        / "generatedRuntimeMessages.ts"
    )


def generate_runtime_messages_typescript() -> str:
    return generate_typescript_interfaces(
        _RuntimeMessageBase,
        raw_type_mapping={
            RuntimeSceneMessage: 'import("../binary").RuntimeMessage',
        },
    )


def write_generated_runtime_messages() -> Path:
    output_path = generated_runtime_messages_path()
    output_path.write_text(generate_runtime_messages_typescript())
    return output_path


if __name__ == "__main__":
    write_generated_runtime_messages()
