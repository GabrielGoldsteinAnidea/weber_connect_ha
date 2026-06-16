"""weber_connect: a clean-room client for the Weber Connect cloud API.

Reverse-engineered from decrypted app traffic. See docs/PROTOCOL.md.
"""
from .client import (
    WeberConnectClient,
    WeberConnectError,
    Token,
    from_config,
    API_HOST,
    MSG_HOST,
)
from .protocol import (
    parse_http_bodies,
    ws_frames,
    parse_companion_frame,
    companion_temps,
    discover_from_ws_stream,
    parse_appliance_status,
    deci_c_to_f,
    PROBE_STATE,
)

__version__ = "0.1.0"
__all__ = [
    "WeberConnectClient", "WeberConnectError", "Token", "from_config",
    "API_HOST", "MSG_HOST",
    "parse_http_bodies", "ws_frames", "parse_companion_frame", "companion_temps",
    "discover_from_ws_stream", "parse_appliance_status", "deci_c_to_f", "PROBE_STATE",
]
