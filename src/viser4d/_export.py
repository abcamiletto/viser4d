import sys
from typing import TYPE_CHECKING

from rich.progress import track

from . import _viser_private as impl
from ._hybrid import stored_message_as_serializable_dict

if TYPE_CHECKING:
    import viser.infra

    from ._server import Viser4dServer


class ExportBuilder:
    """Serialize the current timeline with viser's native recording API."""

    def __init__(self, server: "Viser4dServer") -> None:
        self._server = server

    def serialize(
        self, *, start_timestep: int = 0, end_timestep: int | None = None
    ) -> bytes:
        """Build a native `.viser` recording for the requested timestep range."""
        return self._build_serializer(
            start_timestep=start_timestep,
            end_timestep=end_timestep,
        ).serialize()

    def as_html(
        self,
        *,
        dark_mode: bool = False,
        start_timestep: int = 0,
        end_timestep: int | None = None,
    ) -> str:
        """Build standalone HTML for the requested timestep range."""
        return self._build_serializer(
            start_timestep=start_timestep,
            end_timestep=end_timestep,
        ).as_html(dark_mode=dark_mode)

    def _validate_timesteps(
        self,
        *,
        start_timestep: int,
        end_timestep: int | None,
    ) -> tuple[int, int]:
        """Normalize and validate the requested export bounds."""
        start = start_timestep
        end = end_timestep if end_timestep is not None else self._server.num_steps - 1
        if not 0 <= start < self._server.num_steps:
            raise ValueError(
                f"start_timestep must be in [0, {self._server.num_steps - 1}], "
                f"got {start}."
            )
        if not 0 <= end < self._server.num_steps:
            raise ValueError(
                f"end_timestep must be in [0, {self._server.num_steps - 1}], got {end}."
            )
        if start > end:
            raise ValueError(
                "start_timestep must be less than or equal to end_timestep, "
                f"got {start} > {end}."
            )
        return start, end

    def _build_serializer(
        self, *, start_timestep: int, end_timestep: int | None
    ) -> "viser.infra.StateSerializer":
        """Build viser's serializer populated with timeline state for the range."""
        start, end = self._validate_timesteps(
            start_timestep=start_timestep,
            end_timestep=end_timestep,
        )
        serializer = self._server.get_scene_serializer()
        binary_buffers = impl.serializer_binary_buffers(serializer)
        for step in track(
            range(end + 1),
            description="Exporting .viser",
            disable=not sys.stdout.isatty(),
            transient=True,
        ):
            if step > start:
                serializer.insert_sleep(1.0 / self._server.fps)
            for message in self._server._timeline.messages_for_step(step):
                impl.append_serializer_message(
                    serializer,
                    stored_message_as_serializable_dict(
                        message,
                        binary_buffers=binary_buffers,
                    ),
                )
        return serializer
