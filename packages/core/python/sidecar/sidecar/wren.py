"""Compatibility exports for the lazy Wren context adapter."""

from .wren_adapter import (
    WREN_PACKAGE_NAME,
    WREN_SUPPORTED_VERSION,
    LazyWrenAdapter,
    WrenAdapter,
    default_dependencies,
)

WrenContextAdapter = LazyWrenAdapter
WrenProjectValidator = LazyWrenAdapter

__all__ = [
    "WREN_PACKAGE_NAME",
    "WREN_SUPPORTED_VERSION",
    "LazyWrenAdapter",
    "WrenAdapter",
    "WrenContextAdapter",
    "WrenProjectValidator",
    "default_dependencies",
]

