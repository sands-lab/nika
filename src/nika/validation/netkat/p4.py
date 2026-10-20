"""Check the concrete BMv2 forwarding action used by the restricted projection."""

import json
from pathlib import Path
from uuid import uuid4

import docker

from nika.net_env.p4_dc_fabric.topology_model import SWITCH_IMAGE


def compile_forwarding_slice(program: Path, output: Path):
    output.mkdir(parents=True, exist_ok=True)
    client = docker.from_env()
    container = client.containers.create(
        SWITCH_IMAGE,
        command=[
            "p4c-bm2-ss",
            "--std",
            "p4-16",
            "-o",
            "/output/pipeline.json",
            f"/input/{program.name}",
        ],
        name=f"nika-p4-compile-{uuid4().hex[:12]}",
        network_disabled=True,
        mem_limit="2g",
        nano_cpus=2_000_000_000,
        volumes={
            str(program.parent): {"bind": "/input", "mode": "ro"},
            str(output.resolve()): {"bind": "/output", "mode": "rw"},
        },
    )
    try:
        container.start()
        status = container.wait(timeout=120)["StatusCode"]
        log = container.logs().decode(errors="replace")
        (output / "compiler.log").write_text(log)
        if status:
            raise RuntimeError("P4 compilation failed; see compiler.log")
    finally:
        container.remove(force=True)
        client.close()
    ir = json.loads((output / "pipeline.json").read_text())
    actions = [a for a in ir["actions"] if a["name"].endswith(".ipv4_forward")]
    if len(actions) != 1:
        raise ValueError("P4 projection requires one ipv4_forward action")
    action = actions[0]
    if [a["bitwidth"] for a in action["runtime_data"]] != [48, 48, 9]:
        raise ValueError("Unsupported P4 forwarding action parameters")
    writes = {}
    for primitive in action["primitives"]:
        if primitive["op"] != "assign":
            raise ValueError("Unsupported primitive in P4 forwarding action")
        target, value = primitive["parameters"]
        if target["type"] != "field" or tuple(target["value"]) in writes:
            raise ValueError("Unsupported or repeated P4 field assignment")
        writes[tuple(target["value"])] = value
    expected = {
        ("standard_metadata", "egress_spec"): {"type": "runtime_data", "value": 2},
        ("ethernet", "srcAddr"): {"type": "runtime_data", "value": 0},
        ("ethernet", "dstAddr"): {"type": "runtime_data", "value": 1},
        ("ipv4", "ttl"): {
            "type": "expression",
            "value": {
                "type": "expression",
                "value": {
                    "op": "&",
                    "left": {
                        "type": "expression",
                        "value": {
                            "op": "+",
                            "left": {"type": "field", "value": ["ipv4", "ttl"]},
                            "right": {"type": "hexstr", "value": "0xff"},
                        },
                    },
                    "right": {"type": "hexstr", "value": "0xff"},
                },
            },
        },
    }
    if writes != expected:
        raise ValueError(
            "P4 forwarding action is outside the supported port/MAC/TTL semantics"
        )
    tables = [
        t
        for p in ir["pipelines"]
        for t in p["tables"]
        if t["name"].endswith(".ipv4_lpm")
    ]
    if len(tables) != 1 or [
        (k["target"], k["match_type"])
        for k in tables[0]["key"]
        if k["match_type"] != "selector"
    ] != [(["ipv4", "dstAddr"], "lpm")]:
        raise ValueError(
            "P4 forwarding table needs the supported IPv4 destination LPM key"
        )
    return {
        "action": action["name"],
        "table": tables[0]["name"],
        "scope": "Compiled LPM key and forwarding action; full P4 pipeline semantics are outside this projection",
    }
