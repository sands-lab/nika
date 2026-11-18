"""Prepare an owned Incus base with the host's offline Kathara plugin.

Use the same native hub binary that Firecracker installs from NIKA's image.
Keep the source base unchanged and save the prepared copy as a stopped snapshot.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import subprocess
import shutil
import tempfile
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    from experiment.incus.incus import incus, guest, instance_exists
    from experiment.incus.golden import wait_agent
    from nika.net_env.utils.kathara.docker_files.docker_images import nika_image
    from nika.firecracker.base import _host_plugins

    if instance_exists(args.name):
        parser.error("The destination instance already exists")
    args.out.mkdir(parents=True, exist_ok=True)
    plugin_name = _host_plugins()[0]
    plugin = json.loads(
        subprocess.check_output(["docker", "plugin", "inspect", plugin_name])
    )[0]
    docker_root = subprocess.check_output(
        ["docker", "info", "--format", "{{.DockerRootDir}}"], text=True
    ).strip()
    image = nika_image("vde-switch")
    created = None
    owned = False
    try:
        with tempfile.TemporaryDirectory(prefix="nika-healthy-plugin-") as temporary:
            context = Path(temporary)
            (context / "config.json").write_text(json.dumps(plugin["Config"]))
            package = args.out.resolve() / "plugin-context.tar"
            with package.open("wb") as stream:
                subprocess.run(
                    [
                        "sudo",
                        "-n",
                        "tar",
                        "-C",
                        f"{docker_root}/plugins/{plugin['Id']}/rootfs",
                        "--transform=flags=rh;s,^,rootfs/,",
                        "-cf",
                        "-",
                        ".",
                    ],
                    stdout=stream,
                    check=True,
                )
            created = subprocess.check_output(
                ["docker", "create", "--network", "none", image], text=True
            ).strip()
            binary = args.out.resolve() / "vde_switch"
            subprocess.run(
                ["docker", "cp", f"{created}:/vde_switch", str(binary)], check=True
            )
            target = context / "rootfs/usr/local/bin/vde_switch"
            target.parent.mkdir(parents=True)
            shutil.copy2(binary, target)
            subprocess.run(
                [
                    "tar",
                    "--append",
                    "--file",
                    str(package),
                    "--owner=0",
                    "--group=0",
                    "-C",
                    str(context),
                    "config.json",
                    "rootfs/usr/local/bin/vde_switch",
                ],
                check=True,
            )
            owned = True
            incus("copy", args.source, args.name)
            incus("start", args.name)
            wait_agent(args.name)
            guest(args.name, "systemctl start docker && docker info >/dev/null")
            incus("file", "push", str(package), f"{args.name}/tmp/healthy-plugin.tar")
            command = (
                "mkdir -p /tmp/healthy-plugin && tar -xf /tmp/healthy-plugin.tar -C /tmp/healthy-plugin && "
                f"docker plugin create {shlex.quote(plugin_name)} /tmp/healthy-plugin && "
                f"docker plugin enable {shlex.quote(plugin_name)} && sync"
            )
            guest(args.name, command)
            receipt = {
                "source": args.source,
                "base": f"{args.name}/installed",
                "plugin": plugin_name,
                "host_plugin_id": plugin["Id"],
                "native_hub_image": image,
                "native_hub_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                "package_sha256": hashlib.sha256(package.read_bytes()).hexdigest(),
            }
            incus("stop", args.name)
            incus("snapshot", "create", args.name, "installed")
            receipt["snapshot"] = json.loads(
                incus("query", f"/1.0/instances/{args.name}/snapshots/installed")
            )
            (args.out / "receipt.json").write_text(json.dumps(receipt, indent=2))
    except BaseException:
        if owned:
            incus("delete", "--force", args.name)
        raise
    finally:
        if created:
            subprocess.run(["docker", "rm", "-f", created], check=True)


if __name__ == "__main__":
    main()
