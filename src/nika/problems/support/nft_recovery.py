"""Remove only the nftables rules added by a fault instance."""

import json

from nika.service.lab.nft_api import _NFT_PATH, nft_drop_chains


class NftDropRecovery:
    def __init__(self):
        self.rules = []
        self.tracking_complete = True

    @staticmethod
    def _handles(runtime, node, family):
        raw = runtime.exec(
            node,
            _NFT_PATH
            + "if command -v nft >/dev/null; then nft -j list ruleset; else echo '{\"nftables\":[]}'; fi",
        )
        entries = json.loads(raw).get("nftables", [])
        return {
            (r["rule"]["chain"], int(r["rule"]["handle"]))
            for r in entries
            if "rule" in r
            and r["rule"].get("family") == family
            and r["rule"].get("table") == "filter"
        }

    def add(self, runtime, node, rule, *, family="inet"):
        before = self._handles(runtime, node, family)
        try:
            runtime.add_nft_drop_rule(node, rule, family=family)
        finally:
            try:
                after = self._handles(runtime, node, family)
            except Exception:
                self.tracking_complete = False
                raise
            self.rules.extend(
                (node, family, chain, handle)
                for chain, handle in after - before
                if chain in nft_drop_chains(family)
            )

    def restore(self, runtime):
        for node, family, chain, handle in reversed(self.rules):
            if (chain, handle) in self._handles(runtime, node, family):
                runtime.exec(
                    node,
                    _NFT_PATH
                    + f"nft delete rule {family} filter {chain} handle {handle}",
                )
        return self.tracking_complete and all(
            (chain, handle) not in self._handles(runtime, node, family)
            for node, family, chain, handle in self.rules
        )
