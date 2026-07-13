"""Vendor abstraction seam: Protocol + Backend descriptor + registry. No SDK imports."""
import importlib
from dataclasses import dataclass
from typing import Callable, List, Optional, Protocol, Tuple, runtime_checkable


@dataclass
class ProbeResult:
    channel: int
    stream: str
    ok: bool
    codec: Optional[str]
    width: int
    height: int


@runtime_checkable
class SdkClient(Protocol):
    channel_count: int
    def init(self) -> None: ...
    def login(self, host: str, port: int, username: str, password: str) -> int: ...
    def start_realplay(self, channel: int, play_type: int,
                       on_raw: Callable[[bytes], None]) -> int: ...
    def stop_realplay(self, handle: int) -> None: ...
    def logout(self) -> None: ...
    def cleanup(self) -> None: ...


@dataclass(frozen=True)
class Backend:
    vendor: str
    stream_types: dict
    default_port: int
    ffmpeg_input_args: Tuple[str, ...]
    client_factory: Callable[[], SdkClient]
    list_channels: Callable[[SdkClient], List[int]]
    probe: Callable[[SdkClient, List[Tuple[int, str]], float, int], List[ProbeResult]]


_VENDORS = ("dahua", "hikvision")


def get_backend(vendor: str) -> Backend:
    v = (vendor or "").strip().lower()
    if v not in _VENDORS:
        raise ValueError(f"unknown VENDOR {vendor!r}; valid values: {list(_VENDORS)}")
    return importlib.import_module(f"relay.backends.{v}").BACKEND
