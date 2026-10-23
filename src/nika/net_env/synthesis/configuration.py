"""Install a completed construction through the ordinary environment lifecycle."""


def apply_routing_configs(env, configs: dict[str, str]) -> None:
    """Bind concrete configuration text without invoking a generator at startup.

    The topology, stub hosts and daemon startup remain owned by NIKA. This
    adapter installs native text or SR Linux JSON-IETF configuration trees.
    """
    if env.backend == "containerlab" and getattr(env, "routing_documents", None):
        from nika.net_env.synthesis.srl_updates import srl_config_document

        if set(configs) != set(env.routing_documents):
            raise ValueError(
                "Completed configurations must cover every router exactly once"
            )
        documents = {name: srl_config_document(text) for name, text in configs.items()}
        if isinstance(getattr(type(env), "routing_documents", None), property):
            env._completed_routing_documents = documents
        else:
            env.routing_documents = documents
        return
    if env.backend != "kathara" or getattr(env, "device_profile", "frr") not in {
        "frr",
        "iosxr",
        "routeros",
    }:
        raise ValueError(
            "Completed configuration deployment requires a supported routing backend"
        )
    path = getattr(env, "BGP_CONFIG_PATH", "/etc/frr/frr.conf")
    native = dict(getattr(env, "native_routers", {}))
    expected = {
        name
        for name, machine in env.lab.machines.items()
        if machine.fs and machine.fs.exists(path.lstrip("/"))
    } | set(native)
    if set(configs) != expected or any(
        not isinstance(value, str) or not value.strip() for value in configs.values()
    ):
        raise ValueError(
            "Completed configurations must cover every router exactly once"
        )
    for name, text in configs.items():
        if name in native:
            from nika.net_env.native_routers import native_platform

            # Native routers read their deployable configuration elsewhere.
            config_file = native_platform(native[name]).validation.config_file
            env.lab.machines[name].create_file_from_string(text, config_file)
            continue
        env.lab.machines[name].create_file_from_string(text, path)
    env.deployment_configs = dict(configs)
