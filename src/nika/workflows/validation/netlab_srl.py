"""Bind original negative controls to the generated SR Linux configuration tree."""

from copy import deepcopy
import re

import yaml


def bind_srl_controls(spec, configs):
    """Keep violation sets fixed; select the same native object in actual output."""
    controls = []
    runtime = list(spec.runtime_counterexamples)
    for mutation in spec.counterexamples:
        original = configs[mutation.node]
        # List-item deletion depends on the surrounding YAML indentation.
        # Bind these mutations structurally even when a substring matches.
        if (
            mutation.name
            not in {
                "missing-business-announcement",
                "missing-loopback-announcement",
                "missing-rr-client-role",
                "missing-export",
            }
            and original.count(mutation.old) == 1
        ):
            controls.append(mutation)
            continue
        document = yaml.safe_load(original)
        changed = deepcopy(document)
        (instance,) = changed["srl_nokia-network-instance:network-instance"]
        if mutation.name == "missing-business-announcement":
            (prefix_row,) = yaml.safe_load(mutation.old)
            routing = changed["srl_nokia-routing-policy:routing-policy"]
            business = next(p for p in routing["prefix-set"] if p["name"] == "BUSINESS")
            (row,) = [
                p
                for p in business["prefix"]
                if p["ip-prefix"] == prefix_row["ip-prefix"]
            ]
            business["prefix"].remove(row)
            for index, control in enumerate(runtime):
                if control.name == "business-withdrawal":
                    suffix = "mask-length-range " + row["mask-length-range"]
                    runtime[index] = control.model_copy(
                        update={
                            key: tuple(
                                re.sub(r"mask-length-range \S+", suffix, command)
                                for command in getattr(control, key)
                            )
                            for key in ("commands", "recovery")
                        }
                    )
        elif mutation.name == "missing-loopback-announcement":
            protocol, igp = next(
                (key, value["instance"][0])
                for key, value in instance["protocols"].items()
                if key.endswith(("isis:isis", "ospf:ospf"))
            )
            rows = (
                igp["interface"] if "isis" in protocol else igp["area"][0]["interface"]
            )
            (row,) = [r for r in rows if r["interface-name"] == "system0.0"]
            rows.remove(row)
            if "ospf" in protocol:
                for index, control in enumerate(runtime):
                    if control.name == "loopback-withdrawal":
                        runtime[index] = control.model_copy(
                            update={
                                key: tuple(
                                    command.replace(
                                        "ospf instance default ",
                                        f"ospf instance {igp['name']} ",
                                    )
                                    for command in getattr(control, key)
                                )
                                for key in ("commands", "recovery")
                            }
                        )
        elif mutation.name == "missing-rr-client-role":
            adjacency = next(
                i.adjacency
                for i in spec.healthy.intents
                if i.id in mutation.expected_failed_ids
            )
            bgp = instance["protocols"]["srl_nokia-bgp:bgp"]
            (peer,) = [
                p
                for p in bgp["neighbor"]
                if p["peer-address"] == adjacency.remote_address
            ]
            peer["route-reflector"] = {
                **peer.get("route-reflector", {}),
                "client": False,
            }
        elif mutation.name == "missing-export":
            # Export-policy names and first-match statements originate in the
            # solved fixed sketch; upstream uses rpe_<sequence> statement names.
            (policy_name,) = re.findall(
                r"prefix-set: (NC-OUT-[0-9]+)-[0-9]+", mutation.old
            )
            routing = changed["srl_nokia-routing-policy:routing-policy"]
            actual_name = policy_name.replace("-", "_")
            (policy,) = [p for p in routing["policy"] if p["name"] == actual_name]
            (statement,) = [s for s in policy["statement"] if s["name"] == "rpe_10"]
            if statement["action"] != {"policy-result": "accept"}:
                raise ValueError("Original export-withdrawal binding is not applicable")
            statement["action"]["policy-result"] = "reject"
            for index, control in enumerate(runtime):
                if control.name == "export-withdrawal":
                    runtime[index] = control.model_copy(
                        update={
                            key: tuple(
                                command.replace(policy_name, actual_name).replace(
                                    "statement 10 ", "statement rpe_10 "
                                )
                                for command in getattr(control, key)
                            )
                            for key in ("commands", "recovery")
                        }
                    )
        else:
            raise ValueError(f"Unsupported SR Linux netlab control {mutation.name}")
        controls.append(
            mutation.model_copy(
                update={
                    "old": original,
                    "new": yaml.safe_dump(changed, sort_keys=False),
                }
            )
        )
    return spec.model_copy(
        update={
            "counterexamples": tuple(controls),
            "runtime_counterexamples": tuple(runtime),
        }
    )
