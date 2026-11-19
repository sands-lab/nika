"""Admit redundant link pairs only when their data links form a two-edge cut."""

from dataclasses import replace

import pytest
import yaml

from nika.problems.combinations import get_combination
from nika.problems.rca.inventory import load_offline_net_env
from nika.telemetry import TELEMETRY_KINDS
from nika.telemetry.containerlab import inject_collector
from nika.validation.symptom.combinations import redundant_link_pair
from nika.workflows.benchmark import generate

pytestmark = pytest.mark.contract


@pytest.mark.parametrize("task", get_combination("redundant_links_both_lost").tasks)
def test_catalog_pairs_preserve_connectivity_alone_and_isolate_hosts_together(task):
    env = load_offline_net_env(
        task["scenario"],
        task["topo_size"],
        **{
            key: task[key]
            for key in ("igp", "bgp_mode", "rpki", "backend", "device_profile")
            if key in task
        },
    )
    assert redundant_link_pair(env, task["inject"])
    first = task["inject"]["link_down[1]"]
    assert not redundant_link_pair(env, {"link_down[1]": first, "link_down[2]": first})


def test_telemetry_links_do_not_provide_data_path_redundancy(tmp_path):
    task = get_combination("redundant_links_both_lost").tasks[-1]
    env = load_offline_net_env(
        task["scenario"],
        task["topo_size"],
        **{
            key: task[key]
            for key in ("igp", "bgp_mode", "rpki", "backend", "device_profile")
        },
    )
    doc = env._build_clab_document(lab_name="topology-check")
    inject_collector(doc, TELEMETRY_KINDS, gnmic=None)
    env.topology_file = tmp_path / "topology.yaml"
    env.topology_file.write_text(yaml.safe_dump(doc))
    assert redundant_link_pair(env, task["inject"])
    invalid = {
        "link_down[1]": {"host_name": "berlin", "intf_name": "e1-1"},
        "link_down[2]": {"host_name": "berlin", "intf_name": "e1-2"},
    }
    assert not redundant_link_pair(env, invalid)
    bridge = {"host_name": "pc_ip", "intf_name": "eth1"}
    assert not redundant_link_pair(env, {**task["inject"], "link_down[1]": bridge})


def test_generation_reports_and_omits_nonisolating_pair(monkeypatch):
    combination = get_combination("redundant_links_both_lost")
    task = {
        **combination.tasks[-1],
        "inject": {
            "link_down[1]": {"host_name": "berlin", "intf_name": "e1-1"},
            "link_down[2]": {"host_name": "berlin", "intf_name": "e1-2"},
        },
    }
    monkeypatch.setattr(
        generate, "list_combinations", lambda: [replace(combination, tasks=(task,))]
    )
    rejected = []
    assert generate.build_combination_documents(rejected=rejected) == []
    assert len(rejected) == 1
    assert rejected[0]["reason"] == "combination_topology_precondition"
