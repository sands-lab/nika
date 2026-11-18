"""Bind original negative controls to the generated SR Linux configuration tree."""

import re

import yaml


def bind_srl_runtime(controls, configs):
    """Address the generated SR Linux objects in native runtime controls.

    Static controls mutate the design rendering that Batfish analyzes; these
    commands act on the same objects in the deployed SR Linux configuration.
    """
    bound = []
    for control in controls:
        document = yaml.safe_load(configs[control.node])
        text = "\n".join((*control.commands, *control.recovery))
        rewrite = None
        if control.name == "business-withdrawal":
            prefix = re.search(r"prefix (\S+) mask-length-range \S+", text)[1]
            routing = document["srl_nokia-routing-policy:routing-policy"]
            business = next(p for p in routing["prefix-set"] if p["name"] == "BUSINESS")
            (row,) = [p for p in business["prefix"] if p["ip-prefix"] == prefix]
            suffix = "mask-length-range " + row["mask-length-range"]

            def rewrite(command):
                return re.sub(r"mask-length-range \S+", suffix, command)

        elif control.name == "loopback-withdrawal" and "ospf instance " in text:
            (instance,) = document["srl_nokia-network-instance:network-instance"]
            name = instance["protocols"]["srl_nokia-ospf:ospf"]["instance"][0]["name"]

            def rewrite(command):
                return re.sub(r"ospf instance \S+ ", f"ospf instance {name} ", command)

        elif control.name == "export-withdrawal":
            # Export-policy names and first-match statements originate in the
            # solved fixed sketch; upstream uses rpe_<sequence> statement names.
            policy_name = re.search(r"policy (NC-OUT-[0-9]+) ", text)[1]
            actual_name = policy_name.replace("-", "_")
            routing = document["srl_nokia-routing-policy:routing-policy"]
            (policy,) = [p for p in routing["policy"] if p["name"] == actual_name]
            (statement,) = [s for s in policy["statement"] if s["name"] == "rpe_10"]
            if statement["action"] != {"policy-result": "accept"}:
                raise ValueError("Original export-withdrawal binding is not applicable")

            def rewrite(command):
                return command.replace(policy_name, actual_name).replace(
                    "statement 10 ", "statement rpe_10 "
                )

        if rewrite is not None:
            control = control.model_copy(
                update={
                    key: tuple(rewrite(c) for c in getattr(control, key))
                    for key in ("commands", "recovery")
                }
            )
        bound.append(control)
    return tuple(bound)
