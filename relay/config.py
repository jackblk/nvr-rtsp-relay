"""Proxy configuration: device connection + RTSP target."""
from dataclasses import dataclass
from urllib.parse import quote

from relay.backends.base import DEFAULT_PORTS

_TRUTHY = {"1", "true", "yes", "on"}

# Accept a few friendly spellings for the vendor; normalize to the canonical backend name.
_VENDOR_ALIASES = {"hik": "hikvision", "hikvision": "hikvision", "dahua": "dahua"}


def _is_truthy(val: str) -> bool:
    return (val or "").strip().lower() in _TRUTHY


def _normalize_vendor(val: str) -> str:
    v = (val or "dahua").strip().lower()
    return _VENDOR_ALIASES.get(v, v)


@dataclass(frozen=True)
class Config:
    host: str
    username: str
    password: str
    port: int = 37777
    target_host: str = "0.0.0.0"
    target_port: int = 8554
    # Optional RTSP auth: when both are set, MediaMTX requires these credentials
    # to publish and to view, and the proxy publishes using them.
    target_username: str = ""
    target_password: str = ""
    on_demand: bool = False
    vendor: str = "dahua"
    hikvision_transport: str = "python"

    @classmethod
    def from_env(cls, env: dict) -> "Config":
        missing = [k for k in ("HOST", "USERNAME", "PASSWORD") if not env.get(k)]
        if missing:
            raise ValueError(f"Missing required env var(s): {', '.join(missing)}")
        vendor = _normalize_vendor(env.get("VENDOR"))
        if vendor not in DEFAULT_PORTS:
            raise ValueError(f"unknown VENDOR {vendor!r}; valid values: {list(DEFAULT_PORTS)}")
        hikvision_transport = (env.get("HIKVISION_TRANSPORT") or "python").strip().lower()
        if vendor == "hikvision" and hikvision_transport not in ("python", "sdk"):
            raise ValueError("HIKVISION_TRANSPORT must be python or sdk")
        default_port = DEFAULT_PORTS[vendor]
        return cls(
            host=env["HOST"],
            username=env["USERNAME"],
            password=env["PASSWORD"],
            port=int(env["HOST_PORT"]) if env.get("HOST_PORT") else default_port,
            target_host=env.get("TARGET_HOST") or "0.0.0.0",
            target_port=int(env.get("TARGET_PORT") or 8554),
            target_username=env.get("TARGET_USERNAME") or "",
            target_password=env.get("TARGET_PASSWORD") or "",
            on_demand=_is_truthy(env.get("ON_DEMAND", "")),
            vendor=vendor,
            hikvision_transport=hikvision_transport,
        )

    @property
    def rtsp_auth_enabled(self) -> bool:
        return bool(self.target_username and self.target_password)

    def _userinfo(self) -> str:
        if not self.rtsp_auth_enabled:
            return ""
        # URL-encode so passwords with @ : / # etc. don't corrupt the URL.
        return f"{quote(self.target_username, safe='')}:{quote(self.target_password, safe='')}@"

    def publish_url(self, name: str) -> str:
        # Where the proxy PUSHES the stream: the co-located MediaMTX, always over
        # loopback. target_host is a bind/advertise address (e.g. 0.0.0.0) and is not
        # a valid connect target, so it must not be used here.
        return f"rtsp://{self._userinfo()}127.0.0.1:{self.target_port}/{name}"

    @property
    def viewer_host(self) -> str:
        # target_host doubles as MediaMTX's bind address. Bind-all addresses
        # (0.0.0.0 / ::) aren't connectable by a player, so advertise localhost.
        if self.target_host in ("0.0.0.0", "::", ""):
            return "localhost"
        return self.target_host

    def viewer_url(self, name: str) -> str:
        # Where clients CONNECT to watch: the advertised host/port.
        return f"rtsp://{self._userinfo()}{self.viewer_host}:{self.target_port}/{name}"
