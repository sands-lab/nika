"""Shared XRd Control Plane image and Kathara machine helpers."""

from __future__ import annotations

from nika.net_env.utils.kathara.docker_files.docker_images import image_exists

# Cisco licensing blocks redistributing / auto-building this image like nika/*.
IMAGE = "ios-xr/xrd-control-plane:26.2.1"

CONFIG_FILE_PATH = "disk0:/startup-config.cfg"
XR_ZTP_DISABLE_ENV = "XR_ZTP_ENABLE=0"

# Startup scripts run inside the container shell; double-quoted xr_cli args are fine.
_CLI_COMMAND = '/pkg/bin/xr_cli "{command}"'
_ZTP_APPLY_COMMAND = "/bin/bash -c 'source /pkg/bin/ztp_helper.sh; xrapply {file}'"


def build_xr_interfaces_env(*linux_to_xr: tuple[str, str]) -> str:
    """Build ``XR_INTERFACES`` for eth→XR name mapping.

    Each entry is ``(linux_iface, xr_iface)``, e.g. ``("eth0", "GigabitEthernet0/0/0/0")``.
    """
    parts = [f"linux:{linux},xr_name={xr_name}" for linux, xr_name in linux_to_xr]
    return "XR_INTERFACES=" + ";".join(parts)


def build_xr_startup_script(
    config_path: str = CONFIG_FILE_PATH,
    *,
    wait_iface: str,
) -> list[str]:
    """Kathara ``.startup`` lines: wait for XR, then ``xrapply`` with netns retries."""
    check_cmd_1 = (
        _CLI_COMMAND.format(command="show run")
        + " | egrep -e 'No configuration change' -e 'No such file or directory'"
    )
    check_cmd_2 = (
        _CLI_COMMAND.format(command=f"sh ip interface {wait_iface}")
        + " | egrep -e 'ipv4 protocol is Down'"
    )
    check_cmd_3 = (
        _CLI_COMMAND.format(command=f"sh ipv6 interface {wait_iface}")
        + " | egrep -e 'ipv6 protocol is Down'"
    )

    # xrapply can fail with "Cannot open network namespace" before XR creates
    # the "xrnns" netns; retry until that specific error clears.
    apply_cmd = _ZTP_APPLY_COMMAND.format(file=config_path)
    apply_with_retry = "\n".join(
        [
            "apply_ok=0",
            "for _xr_apply_try in $(seq 1 40); do",
            f"  _xr_apply_out=$({apply_cmd} 2>&1)",
            '  echo "$_xr_apply_out"',
            '  if ! echo "$_xr_apply_out" | grep -q "Cannot open network namespace"; then',
            "    apply_ok=1",
            "    break",
            "  fi",
            "  sleep 3",
            "done",
            '[[ "$apply_ok" -eq 1 ]] || echo "ERROR: xrapply did not succeed after retries"',
        ]
    )

    return [
        "pgrep xrd-startup; while [[ $? -eq 0 ]]; do sleep 3; pgrep xrd-startup; done",
        check_cmd_1,
        f"while [[ $? -eq 0 ]]; do sleep 3; {check_cmd_1}; done",
        check_cmd_2,
        f"while [[ $? -eq 0 ]]; do sleep 3; {check_cmd_2}; done",
        check_cmd_3,
        f"while [[ $? -eq 0 ]]; do sleep 3; {check_cmd_3}; done",
        "source /pkg/bin/ztp_helper.sh; ztp_disable; ztp_kill_all; killall -9 pyztp2",
        apply_with_retry,
    ]


def require_xrd_image(image: str = IMAGE) -> None:
    """Raise if the local XRd Control Plane image is missing."""
    if image_exists(image):
        return
    raise RuntimeError(
        f"XRd Control Plane image {image!r} not found locally. Cisco's "
        "license requires loading it by hand, e.g.:\n"
        "  docker load -i xrd-control-plane-container-x86.<version>.tgz\n"
        f"  docker tag <loaded-tag> {image}"
    )
