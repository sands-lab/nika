"""Install a completed construction through the ordinary environment lifecycle."""


def apply_routing_configs(env, configs: dict[str, str]) -> None:
    """Bind concrete configuration text without invoking a generator at startup.

    The topology, stub hosts and daemon startup remain owned by NIKA. This
    adapter currently accepts the native text formats exercised by netlab ISP
    construction, rather than silently ignoring configurations on other labs.
    """
    if (
        env.backend != "kathara"
        or getattr(env, "device_profile", None) not in {"frr", "iosxr"}
        or getattr(env, "plan", None) is None
        or not hasattr(env, "deployment_configs")
    ):
        raise ValueError(
            "Completed configuration deployment requires a Kathara FRR/IOS XR ISP"
        )
    if set(configs) != set(env.deployment_configs) or any(
        not isinstance(value, str) or not value.strip() for value in configs.values()
    ):
        raise ValueError(
            "Completed configurations must cover every router exactly once"
        )
    path = getattr(env, "BGP_CONFIG_PATH", "/etc/frr/frr.conf")
    for name, text in configs.items():
        env.lab.machines[name].create_file_from_string(text, path)
    env.deployment_configs = dict(configs)
