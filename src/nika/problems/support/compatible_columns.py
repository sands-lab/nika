"""Coverage-column sets shared by several failures' ``COMPATIBLE_COLUMNS``."""

from __future__ import annotations

# Columns where Linux IP forwarding can emit ICMP Fragmentation Needed.
LINUX_PMTU_COLUMNS = frozenset(
    {
        "dc_clos",
        "campus_lan",
        "enterprise_branch",
        "k8s_lab",
        "isp_abilene/isis",
        "isp_abilene/ospf",
        "isp_abilene/ibgp_rr",
        "isp_abilene_ebgp_rpki",
        "isp_geant_ebgp_rpki",
        "isp_abilene_ebgp_rtbh",
    }
)

# Router platforms that forward in the Linux kernel on every scenario they
# deploy, so Linux PMTU behaviour holds there too. SONiC-VS routes with FRR and
# forwards through kernel ports that carry the CONFIG_DB MTU. SR Linux forwards
# in its own datapath and is not one of them.
LINUX_PMTU_PLATFORMS = frozenset({"sonic"})

# Host-traffic columns outside the Kubernetes labs (k8s_lab, llmd_lab).
NON_K8S_HOST_COLUMNS = frozenset(
    {
        "campus_lan",
        "dc_clos",
        "enterprise_branch",
        "p4_dc_fabric",
        "p4_dc_gateway",
        "sdn_l3_clos",
    }
)
