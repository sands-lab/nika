"""Collect healthy scenarios through NIKA's administrative execution interface.

Reuse convergence sampling and lifecycle contexts. Keep administrative exports
separate from the access policy used by diagnosis agents.
"""

from __future__ import annotations

import asyncio
import argparse
import base64
import functools
import gzip
import json
import os
import shlex
import signal
from pathlib import Path

from experiment.convergence import collect


@functools.lru_cache(maxsize=8)
def local_api(session_id: str):
    from nika.runtime.factory import resolve_backend
    from nika.service.lab.host_api import create_host_api
    from nika.workflows.session.close import load_session_meta_for_close

    meta = load_session_meta_for_close(session_id)
    return create_host_api(
        lab_name=meta["lab_name"], backend=resolve_backend(meta), session_meta=meta
    )


def guest_command(command: str, timeout: float = 900) -> str | None:
    from nika.firecracker.trial import active_trial_lab
    from nika.firecracker.vmm import VSOCK, guest_exec

    lab = active_trial_lab()
    if lab is not None:
        return guest_exec(lab.dir / VSOCK, command, timeout=timeout)
    from experiment.incus.trial import active_trial_lab as active_incus
    from experiment.incus.incus import incus

    lab = active_incus()
    if lab is not None:
        return incus(
            "exec",
            lab.golden.name,
            "--",
            "sh",
            "-c",
            command,
            project=lab.project,
            timeout=timeout,
        )
    return None


class AdministrativeTool:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.api = local_api(session_id)

    async def ainvoke(self, params: dict) -> str:
        result = await asyncio.to_thread(
            self.api.exec_cmd,
            params["host_name"],
            params["command"],
            params.get("timeout", 60),
        )
        if result.startswith("[TIMEOUT]"):
            raise TimeoutError(result)
        return result


async def dump_node(tool, node: str, script: str = collect.SCRIPT) -> dict:
    encoded = base64.b64encode(gzip.compress(script.encode())).decode()
    raw = await tool.ainvoke(
        {
            "host_name": node,
            "command": f"printf %s {shlex.quote(encoded)} | base64 -d | gzip -d | sh",
            "timeout": 300,
        }
    )
    if script.startswith(collect.SCRIPT) and "@@@ " not in raw:
        raise RuntimeError(
            f"Administrative export returned no tagged state: {raw[:500]}"
        )
    return {"raw": raw}


async def dump_session(
    session_id: str, scenario: str, probe: bool = False, tools_path: Path | None = None
) -> dict:
    from nika.firecracker.base import GUEST_REPO
    from experiment.healthy_scenarios.cpu import audit

    cpu_audit = audit(session_id)

    code = (
        "import asyncio,base64,gzip,json; "
        "from experiment.healthy_scenarios.collect import dump_session; "
        f"data=asyncio.run(dump_session({session_id!r},{scenario!r},probe={probe!r})); "
        "print('__NIKA_HEALTHY_STATE__'+base64.b64encode(gzip.compress(json.dumps(data).encode())).decode()+'__NIKA_HEALTHY_END__')"
    )
    encoded = base64.b64encode(code.encode()).decode()
    command = "su - ubuntu -c " + shlex.quote(
        f"cd {shlex.quote(GUEST_REPO)} && PYTHONPATH=src .venv/bin/python -c "
        + shlex.quote(f"import base64; exec(base64.b64decode({encoded!r}))")
    )
    remote = guest_command(command)
    if remote is not None:
        _before, marker, payload = remote.rpartition("__NIKA_HEALTHY_STATE__")
        if not marker:
            raise RuntimeError(
                f"Guest export returned no state payload: {remote[-2000:]}"
            )
        if tools_path:
            tools_path.write_text(
                json.dumps(
                    {
                        "interface": "batched NIKA administrative exec",
                        "cpu_alignment": cpu_audit,
                    }
                )
            )
            phases = guest_command(
                f"cat {shlex.quote(GUEST_REPO)}/runtime/healthy-phases.jsonl"
            )
            tools_path.with_suffix(".guest-phases.jsonl").write_text(phases)
        payload, end, _after = payload.partition("__NIKA_HEALTHY_END__")
        if not end:
            raise RuntimeError("Guest export returned an incomplete state payload")
        return json.loads(gzip.decompress(base64.b64decode(payload.strip())))

    from nika.workflows.session.containers import list_session_containers
    from nika.workflows.session.close import (
        load_session_meta_for_close,
        session_net_env,
    )
    from experiment.healthy_scenarios.exports import export_script
    from experiment.healthy_scenarios.normalize import normalize

    collect._dump_node = dump_node
    _sid, _lab, rows = list_session_containers(session_id)
    environment = session_net_env(load_session_meta_for_close(session_id))
    identities = environment.machine_identities
    nodes = sorted({str(row.get("name") or "") for row in rows} - {""})
    tool = AdministrativeTool(session_id)
    sem = asyncio.Semaphore(8)

    async def one(node: str) -> tuple[str, dict]:
        async with sem:
            try:
                identity = identities.get(node)
                capabilities = identity.capabilities if identity else ()
                model, extra = export_script(capabilities)
                value = await collect._dump_node(tool, node, collect.SCRIPT + extra)
                value.update(
                    forwarding_model=model,
                    capabilities=list(capabilities),
                    per_flow_probe="linux kernel ECMP"
                    if model == "linux"
                    else "unsupported",
                )
                try:
                    value["normalized_forwarding"] = normalize(model, value["raw"])
                except Exception as exc:
                    value["normalization_error"] = repr(exc)
                return node, value
            except Exception as exc:
                return node, {"error": repr(exc)}

    results = dict(await asyncio.gather(*(one(node) for node in nodes)))
    if probe:
        await collect._probe_nodes(
            tool,
            {
                name: value
                for name, value in results.items()
                if value.get("forwarding_model") == "linux"
            },
            sem,
        )
    if tools_path:
        tools_path.write_text(
            json.dumps(
                {"interface": "nika administrative exec", "cpu_alignment": cpu_audit}
            )
        )
    return results


def main() -> None:
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--mode")
    options, _ = parser.parse_known_args()
    from experiment.healthy_scenarios.cpu import alignment

    with alignment(options.mode):
        if os.environ.get("NIKA_HEALTHY_PHASE_LOG"):
            from experiment.healthy_scenarios.phases import install
            from experiment.healthy_scenarios.guest_probe import (
                install as install_guests,
            )

            install()
            install_guests()
        collect._dump_session = dump_session
        collect._dump_node = dump_node
        collect.main()


if __name__ == "__main__":
    main()
