"""Reuse live-state parsers and retain explicit limits for other datapaths."""

from __future__ import annotations

from dataclasses import asdict

from experiment.convergence.parse import parse_json, sections


def normalize(model: str, raw: str) -> dict:
    observed = sections(raw)
    if model in {"linux", "sonic", "srlinux"}:
        from nika.net_env.isp.fib import linux_fib, srl_fib

        parser, label = (
            (srl_fib, "srlfib") if model == "srlinux" else (linux_fib, "route4")
        )
        routes = parser(parse_json(observed.get(label, "")))
        return {
            "scope": "IPv4 default/main and local tables; native parser semantics",
            "routes": [
                asdict(route) | {"prefix": str(route.prefix)} for route in routes
            ],
        }
    if model == "routeros":
        from nika.net_env.utils.routeros.parse import routeros_terse_rows

        return {
            "scope": "native route properties; no complete FIB equivalence model",
            "route_properties": routeros_terse_rows(
                observed.get("routeros_routes", "")
            ),
        }
    if model == "p4runtime" and "p4runtime" in observed:
        from nika.net_env.p4_dc_fabric.fabric_manager.apply import forwarding_entries

        document = parse_json(observed["p4runtime"])
        switches = document["switches"]
        if not switches or any(
            not state.get("pipeline") for state in switches.values()
        ):
            raise ValueError("P4Runtime export lacks an installed pipeline")
        return {
            "scope": "installed IPv4 LPM, selector groups and members",
            "switches": {
                name: {
                    kind: forwarding_entries(state, kind)
                    for kind in ("groups", "members", "ipv4_lpm")
                }
                for name, state in switches.items()
            },
        }
    return {
        "scope": "raw native state retained; normalized FIB equivalence unsupported"
    }
