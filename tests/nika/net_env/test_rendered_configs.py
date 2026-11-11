"""Environments deploy the netlab configurations rendered from their design."""

import pytest

from nika.net_env.net_env_pool import get_net_env_instance
from nika.net_env.synthesis.rendered import StaleRenderedConfigs, stored_configs

pytestmark = pytest.mark.contract


@pytest.mark.parametrize("scenario", ["dc_clos", "campus_lan", "enterprise_branch"])
@pytest.mark.parametrize("size", ["s", "m", "l"])
def test_scenarios_deploy_current_rendered_configs(scenario, size):
    env = get_net_env_instance(scenario, topo_size=size)
    for name, config in stored_configs(env).items():
        assert env.lab.machines[name].fs.readtext("etc/frr/frr.conf") == config


def test_changed_design_rejects_stale_rendered_configs():
    env = get_net_env_instance("dc_clos", topo_size="s", rendered=False)
    env.routing_design.routers[env.model.leaves[0]].bgp_multipath = 8
    with pytest.raises(StaleRenderedConfigs, match="stale"):
        stored_configs(env)


def _isp_variants():
    from nika.config import BENCHMARK_DIR
    from nika.net_env.isp.rendered import benchmark_variants

    pool = BENCHMARK_DIR / "working" / "pool"
    return [
        (path.name, options)
        for path in sorted(pool.glob("isp_*"))
        for options in benchmark_variants(path.name)
    ]


@pytest.mark.parametrize(("scenario", "options"), _isp_variants())
def test_isp_benchmark_variants_deploy_current_rendered_configs(scenario, options):
    from nika.net_env.synthesis.rendered import renderable

    env = get_net_env_instance(scenario, **options)
    if (options.get("device_profile"), options.get("igp")) == ("routeros", "isis"):
        # netlab's RouterOS templates have no IS-IS module; NIKA renders these.
        assert not renderable(env)
        return
    assert renderable(env), "netlab must generate every stored ISP variant"
    stored = stored_configs(env)
    if env.backend == "containerlab":
        from nika.net_env.synthesis.srl_updates import srl_config_document

        stored = {name: srl_config_document(text) for name, text in stored.items()}
        assert env.routing_documents == stored
    else:
        assert env.deployment_configs == stored


@pytest.mark.parametrize(
    ("scenario", "options"),
    [
        ("dc_clos", {"topo_size": "s"}),
        ("campus_lan", {"topo_size": "s", "device_profile": "iosxr"}),
        ("enterprise_branch", {"topo_size": "s"}),
        ("isp_abilene_ebgp_rpki", {}),
        (
            "isp_pdh",
            {
                "igp": "isis",
                "bgp_mode": "ibgp_rr",
                "device_profile": "nokia_srlinux",
                "backend": "containerlab",
            },
        ),
    ],
)
def test_stored_configs_equal_a_fresh_netlab_generation(scenario, options, tmp_path):
    """Generator changes outside the design digest also require a new render."""
    pytest.importorskip("netsim")
    from nika.net_env.synthesis.rendered import _generate

    env = get_net_env_instance(scenario, rendered=False, **options)
    generated = _generate(env, tmp_path)
    stored = stored_configs(env)
    assert dict(generated) == dict(stored)
    assert getattr(generated, "scripts", {}) == stored.scripts
