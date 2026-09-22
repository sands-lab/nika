"""Shared Cisco IOS-XR (XRd) helpers for Kathara labs."""

from nika.net_env.iosxr.common import (
    CONFIG_FILE_PATH,
    IMAGE,
    XR_ZTP_DISABLE_ENV,
    build_xr_interfaces_env,
    build_xr_startup_script,
    require_xrd_image,
)

__all__ = [
    "CONFIG_FILE_PATH",
    "IMAGE",
    "XR_ZTP_DISABLE_ENV",
    "build_xr_interfaces_env",
    "build_xr_startup_script",
    "require_xrd_image",
]
