"""Deploy router configurations that netlab generated offline from the design.

``nika env render`` compiles each variant of an environment's routing design
with netlab and stores every router's configuration under the environment's
``rendered/<variant>`` directory, with a digest of the netlab input and
version. At startup the environment recomputes the digest without running
netlab and deploys the stored configurations; a changed design must be
rendered again.

Environments that build their lab from a :class:`RoutingDesign` use the
defaults here. Other environments provide the same hooks as methods:
``rendered_parts()``, ``netlab_renderable()``, ``netlab_render_input()``,
``render_netlab_configs(directory)`` and the class method
``rendered_variants(scenario)``.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path

from nika.config import pkg_path
from nika.net_env.synthesis.configuration import RouterConfigs

MANIFEST = "manifest.json"


class StaleRenderedConfigs(ValueError):
    """The stored configurations were rendered from a different design."""


def variant(env) -> str:
    """Name the deployment variant whose configurations are stored."""
    return store(env).name


def store(env) -> Path:
    if hasattr(env, "rendered_parts"):
        return pkg_path("net_env", *env.rendered_parts())
    profile = getattr(env, "device_profile", None) or "frr"
    name = env.topo_size + ("" if profile == "frr" else f"-{profile}")
    return pkg_path("net_env", env.LAB_NAME, "rendered", name)


def renderable(env) -> bool:
    """Whether netlab generates every router of this deployment."""
    if hasattr(env, "netlab_renderable"):
        return env.netlab_renderable()
    from nika.net_env.native_routers import native_platform

    return all(
        native_platform(profile).validation.netlab_device is not None
        for profile in getattr(env, "native_routers", {}).values()
    )


def _netlab_input(env) -> str:
    if hasattr(env, "netlab_render_input"):
        return json.dumps(env.netlab_render_input(), sort_keys=True, default=str)
    from nika.net_env.synthesis.netlab_kathara import kathara_topology

    topology, _ = kathara_topology(env)
    payload = json.dumps(topology, sort_keys=True, default=str)
    return f"{payload}\n{sorted(env.routing_design.routers.items())!r}"


def design_digest(env) -> str:
    """Digest of the netlab input, the routing design and the netlab version."""
    from nika.net_env.synthesis.netlab import NETLAB_VERSION

    text = f"{NETLAB_VERSION}\n{_netlab_input(env)}"
    return hashlib.sha256(text.encode()).hexdigest()


def _generate(env, directory: Path) -> dict[str, str]:
    if hasattr(env, "render_netlab_configs"):
        return env.render_netlab_configs(directory)
    from nika.net_env.synthesis.netlab_kathara import generate_kathara_configs

    return generate_kathara_configs(env, None, directory)


def render(env) -> Path:
    """Generate the variant's configurations with netlab and store them."""
    from nika.net_env.synthesis.netlab import NETLAB_VERSION

    with tempfile.TemporaryDirectory() as work:
        configs = _generate(env, Path(work))
    directory = store(env)
    directory.mkdir(parents=True, exist_ok=True)
    for previous in [*directory.glob("*.conf"), *directory.glob("*.sh")]:
        previous.unlink()
    for name, config in sorted(configs.items()):
        (directory / f"{name}.conf").write_text(config)
    scripts = getattr(configs, "scripts", {})
    for name, modules in scripts.items():
        for module, script in modules.items():
            (directory / f"{name}.{module}.sh").write_text(script)
    manifest = {
        "digest": design_digest(env),
        "netlab": NETLAB_VERSION,
        "routers": sorted(configs),
    }
    if scripts:
        manifest["scripts"] = {name: sorted(m) for name, m in sorted(scripts.items())}
    (directory / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n")
    return directory


def _scenario(env) -> str:
    return getattr(env, "scenario_id", None) or env.LAB_NAME


def _label(env) -> str:
    return f"{_scenario(env)} {variant(env)}"


def _command(env) -> str:
    return f"uv run nika env render --scenario {_scenario(env)}"


def is_stored(env) -> bool:
    return (store(env) / MANIFEST).exists()


def stored_configs(env) -> RouterConfigs:
    """Return the stored configurations, or raise when they are missing or stale."""
    directory = store(env)
    if not is_stored(env):
        raise StaleRenderedConfigs(
            f"No rendered configurations for {_label(env)}; run `{_command(env)}`"
        )
    manifest = json.loads((directory / MANIFEST).read_text())
    if manifest["digest"] != design_digest(env):
        raise StaleRenderedConfigs(
            f"Rendered configurations for {_label(env)} are stale; "
            f"run `{_command(env)}`"
        )
    return RouterConfigs(
        {
            name: (directory / f"{name}.conf").read_text()
            for name in manifest["routers"]
        },
        scripts={
            name: {m: (directory / f"{name}.{m}.sh").read_text() for m in modules}
            for name, modules in manifest.get("scripts", {}).items()
        },
    )


def stores_configs(scenario: str) -> bool:
    """Whether the scenario's environment can deploy stored configurations."""
    from importlib import import_module

    from nika.net_env.net_env_pool import list_all_net_envs

    spec = list_all_net_envs()[scenario]
    env_cls = getattr(import_module(spec.module), spec.class_name)
    return getattr(env_cls, "STORES_RENDERED_CONFIGS", False)


def rendered_variants(env_cls, scenario: str) -> list[dict]:
    """Deployment options of every variant whose configurations are stored."""
    if hasattr(env_cls, "rendered_variants"):
        return env_cls.rendered_variants(scenario)
    from nika.net_env.native_routers import native_platform

    variants = [{"topo_size": size} for size in ("s", "m", "l")]
    for profile in getattr(env_cls, "NATIVE_PROFILE_ROLES", {}):
        if native_platform(profile).validation.netlab_device is None:
            continue
        sizes = getattr(env_cls, "NATIVE_PROFILE_SIZES", {}).get(
            profile, ("s", "m", "l")
        )
        variants += [{"topo_size": size, "device_profile": profile} for size in sizes]
    from nika.net_env.net_env_pool import (
        list_all_net_envs,
        scenario_containerlab_device_profiles,
    )

    sizes = list_all_net_envs()[scenario].topo_size
    for profile in scenario_containerlab_device_profiles(scenario):
        variants += [
            {"backend": "containerlab", "topo_size": size, "device_profile": profile}
            for size in (sizes if isinstance(sizes, list) else (None,))
        ]
    return variants


def current_configs(env) -> RouterConfigs | None:
    """Return the stored configurations, or None when the variant has none."""
    if not renderable(env) or not is_stored(env):
        return None
    return stored_configs(env)


def use_rendered_configs(env, *, required: bool = True) -> bool:
    """Deploy the stored netlab configurations instead of NIKA's rendering.

    With ``required=False``, a variant without stored configurations keeps
    NIKA's rendering; stale configurations still raise. Returns whether the
    stored configurations were deployed.
    """
    if not renderable(env) or (not required and not is_stored(env)):
        return False
    from nika.net_env.synthesis.configuration import apply_routing_configs

    apply_routing_configs(env, stored_configs(env))
    return True


def deploy_netlab_scripts(env, parameters=None) -> None:
    """Install netlab's Linux scripts on routers whose configuration NIKA renders.

    Routers that netlab configures partly in Linux, such as WireGuard tunnel
    endpoints, need these scripts whichever generator renders the routing.
    """
    from nika.net_env.synthesis.configuration import NETLAB_SCRIPT_DIR
    from nika.net_env.synthesis.netlab_kathara import generate_kathara_configs

    native = getattr(env, "native_routers", {})
    if not any(
        interface.wireguard
        for router in env.routing_design.routers.values()
        if router.name not in native
        for interface in router.interfaces
    ):
        return
    with tempfile.TemporaryDirectory() as work:
        scripts = generate_kathara_configs(env, parameters, Path(work)).scripts
    for name, modules in scripts.items():
        for module, script in modules.items():
            env.lab.machines[name].create_file_from_string(
                script, f"{NETLAB_SCRIPT_DIR}/{module}.sh"
            )
