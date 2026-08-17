"""Public API for the Wren data-agent sidecar boundary."""

from .dispatch import (
    RPC_METHODS,
    ContextProvider,
    Dispatcher,
    ProjectValidator,
    QueryPlanner,
    RpcDispatcher,
    RpcRequest,
    SidecarDependencies,
    dispatch_request,
)
from .errors import RpcError, RpcFault
from .protocol import (
    DEFAULT_MAX_FRAME_BYTES,
    PROTOCOL_VERSION,
    FramingError,
    ProtocolError,
    decode_frame,
    decode_payload,
    encode_frame,
    read_frame,
    write_frame,
)
from .server import JsonRpcServer, serve
from .wren_adapter import (
    WREN_PACKAGE_NAME,
    WREN_SUPPORTED_VERSION,
    LazyWrenAdapter,
    WrenAdapter,
    default_dependencies,
)

__all__ = [
    "DEFAULT_MAX_FRAME_BYTES",
    "ContextProvider",
    "Dispatcher",
    "FramingError",
    "JsonRpcServer",
    "PROTOCOL_VERSION",
    "RPC_METHODS",
    "ProjectValidator",
    "QueryPlanner",
    "ProtocolError",
    "RpcDispatcher",
    "RpcError",
    "RpcFault",
    "RpcRequest",
    "SidecarDependencies",
    "decode_frame",
    "decode_payload",
    "dispatch_request",
    "encode_frame",
    "read_frame",
    "serve",
    "write_frame",
    "WREN_PACKAGE_NAME",
    "WREN_SUPPORTED_VERSION",
    "LazyWrenAdapter",
    "WrenAdapter",
    "default_dependencies",
]
