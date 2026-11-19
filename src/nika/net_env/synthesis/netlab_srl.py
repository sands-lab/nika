"""Retain SR Linux attributes not emitted by the upstream templates."""

import yaml


def retain_srl_attributes(configs, documents):
    """Keep existing attributes unavailable in the pinned routing templates.

    The IPv4 routing generator owns peers, origins and policies. Preserve the
    sketch's IPv6 interface addressing and explicit IGP ECMP capacity; neither
    requires a second protocol or policy renderer.
    """
    for name, text in configs.items():
        output = yaml.safe_load(text)
        for interface in documents[name]["srl_nokia-interfaces:interface"]:
            for sub in interface["subinterface"]:
                if "ipv6" in sub:
                    output["operations"].append(
                        {
                            "operation": "update",
                            "path": f"/interface[name={interface['name']}]/subinterface[index={sub['index']}]",
                            "value": {"ipv6": sub["ipv6"]},
                        }
                    )
        for instance in documents[name]["srl_nokia-network-instance:network-instance"]:
            for protocol in ("isis", "ospf"):
                value = instance.get("protocols", {}).get(
                    f"srl_nokia-{protocol}:{protocol}", {}
                )
                for igp in value.get("instance", []):
                    if "max-ecmp-paths" in igp:
                        # The pinned OSPF template names its process 1.
                        process_name = "1" if protocol == "ospf" else igp["name"]
                        output["operations"].append(
                            {
                                "operation": "update",
                                "path": f"/network-instance[name={instance['name']}]/protocols/{protocol}/instance[name={process_name}]",
                                "value": {"max-ecmp-paths": igp["max-ecmp-paths"]},
                            }
                        )
        configs[name] = yaml.safe_dump(output, sort_keys=False)
    return configs
