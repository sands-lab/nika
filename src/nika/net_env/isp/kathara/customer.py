"""Kathara attachment of the plain eBGP customer site."""

from nika.runtime.spec import NodeRole


def attach_customer_site(env, host_opts: dict) -> str | None:
    """Attach the declared customer host and return its provider interface."""
    site = env.bgp_plan.customer_site if env.bgp_plan is not None else None
    if site is None:
        return None
    node = next(n for n in env.plan.nodes if n.device_name == site.provider)
    interface = env.bgp_plan.customer_interface(node)
    collision = f"cd_{site.host_name}"
    env.lab.new_machine(site.host_name, **host_opts)
    env.declare_machine(
        site.host_name,
        role=NodeRole.HOST,
        capabilities=("linux",),
        reachability_target=True,
    )
    env.lab.connect_machine_to_link(
        site.provider, collision, machine_iface_number=int(interface[3:])
    )
    env.lab.connect_machine_to_link(site.host_name, collision, machine_iface_number=0)
    plen = site.prefix.rsplit("/", 1)[1]
    env.lab.create_file_from_list(
        [
            f"ip addr add {site.address}/{plen} dev eth0",
            f"ip route replace default via {site.gateway}",
        ],
        f"{site.host_name}.startup",
    )
    return interface
