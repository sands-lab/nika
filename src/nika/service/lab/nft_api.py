"""Shared nftables API for Kathara and Containerlab labs."""

from __future__ import annotations

from nika.service.lab.protocols import SupportsExec

# Some Kathara/docker exec contexts omit sbin from PATH.
_NFT_PATH = "export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin; "
_NFT_PRESENT_CHECK = (
    f"{_NFT_PATH}"
    "if command -v nft >/dev/null 2>&1 || "
    "[ -x /usr/sbin/nft ] || [ -x /sbin/nft ]; then echo OK; else echo MISSING; fi"
)
_NFT_INSTALL_CMD = (
    f"{_NFT_PATH}"
    "command -v nft >/dev/null 2>&1 && exit 0; "
    "if command -v apt-get >/dev/null 2>&1; then "
    "apt-get update -qq && DEBIAN_FRONTEND=noninteractive "
    "apt-get install -y -qq nftables >/dev/null 2>&1; "
    "elif command -v apk >/dev/null 2>&1; then "
    "apk add --no-cache nftables >/dev/null 2>&1; "
    "fi"
)
_NFT_INSTALL_TIMEOUT_SEC = 120.0
_RC_MARK = "__rc="
_DROP_CHAINS = ("input", "forward", "output")


def _normalize_rule(rule: str) -> str:
    """Collapse whitespace and drop the ``# handle N`` suffix of listed rules."""
    return " ".join(rule.split("#", 1)[0].split())


def nft_drop_chains(family: str) -> tuple[str, ...]:
    """Chains ``add_nft_drop_rule`` hooks for ``family`` (arp has no forward)."""
    return ("input", "output") if family == "arp" else _DROP_CHAINS


def nft_list_chain_command(table: str, chain: str, family: str = "inet") -> str:
    """Shell command that prints one chain in nft's canonical rule form."""
    return f"{_NFT_PATH}nft list chain {family} {table} {chain} 2>/dev/null"


def nft_chain_has_rule(listing: str, rule: str) -> bool:
    """Return whether an ``nft list chain`` listing holds exactly ``rule``.

    ``rule`` must be written the way ``nft list`` prints it (e.g.
    ``tcp dport 6653 drop``). A longer rule that merely contains it, or the
    same text in another table or chain, does not match.
    """
    wanted = _normalize_rule(rule)
    return any(_normalize_rule(line) == wanted for line in listing.splitlines())


class NFTableMixin:
    """nftables operations via ``exec_cmd``."""

    def nft_list_ruleset(self: SupportsExec, host_name: str) -> str:
        return self.exec_cmd(host_name, f"{_NFT_PATH}nft -a list ruleset")

    def nft_list_tables(self: SupportsExec, host_name: str) -> str:
        return self.exec_cmd(host_name, f"{_NFT_PATH}nft list tables")

    def nft_list_chains(self: SupportsExec, host_name: str) -> str:
        return self.exec_cmd(host_name, f"{_NFT_PATH}nft list chains")

    def nft_add_table(
        self: SupportsExec,
        host_name: str,
        table_name: str,
        family: str = "inet",
    ) -> str:
        return self.exec_cmd(
            host_name, f"{_NFT_PATH}nft add table {family} {table_name}"
        )

    def nft_add_chain(
        self: SupportsExec,
        host_name: str,
        table: str,
        chain: str,
        family: str = "inet",
        hook: str | None = None,
        type: str | None = None,
        policy: str | None = None,
    ) -> str:
        command = f"{_NFT_PATH}nft add chain {family} {table} {chain}"
        if type and hook:
            command += f" '{{ type {type} hook {hook} priority 0 ;"
            if policy:
                command += f" policy {policy} ;"
            command += " }'"
        return self.exec_cmd(host_name, command)

    def nft_add_rule(
        self: SupportsExec,
        host_name: str,
        table: str,
        chain: str,
        rule: str,
        family: str = "inet",
    ) -> str:
        return self.exec_cmd(
            host_name,
            f"{_NFT_PATH}nft add rule {family} {table} {chain} {rule}",
        )

    def nft_delete_table(
        self: SupportsExec,
        host_name: str,
        table_name: str,
        family: str = "inet",
    ) -> str:
        return self.exec_cmd(
            host_name, f"{_NFT_PATH}nft delete table {family} {table_name}"
        )

    # Orchestrator / runtime semantic aliases
    def list_nft_ruleset(self: SupportsExec, node: str) -> str:
        return self.exec_cmd(node, f"{_NFT_PATH}nft list ruleset 2>/dev/null").strip()

    def _nft_checked(self: SupportsExec, node: str, args: str) -> str:
        """Run ``nft <args>`` and raise when it exits non-zero."""
        raw = self.exec_cmd(
            node, f"{_NFT_PATH}nft {args} 2>&1; printf '\\n{_RC_MARK}%s\\n' $?"
        )
        output, marker, tail = (raw or "").rpartition(_RC_MARK)
        if not marker:
            raise RuntimeError(f"nft {args!r} on {node} returned no exit status")
        try:
            returncode = int(tail.strip().splitlines()[0])
        except (IndexError, ValueError):
            returncode = 1
        if returncode != 0:
            raise RuntimeError(
                f"nft {args!r} failed on {node} (rc={returncode}): {output.strip()!r}"
            )
        return output.strip()

    def _nft_add_chain(
        self: SupportsExec,
        node: str,
        table: str,
        chain: str,
        family: str,
        hook: str,
    ) -> None:
        self._nft_checked(
            node,
            f"add chain {family} {table} {chain} "
            f"'{{ type filter hook {hook} priority 0 ; policy accept ; }}'",
        )

    def _ensure_nft_available(self: SupportsExec, node: str) -> None:
        check = self.exec_cmd(node, _NFT_PRESENT_CHECK)
        if "OK" in (check or ""):
            return
        # Default exec timeout (10s) is too short for apt-get update+install.
        self.exec_cmd(node, _NFT_INSTALL_CMD, timeout=_NFT_INSTALL_TIMEOUT_SEC)
        check = self.exec_cmd(node, _NFT_PRESENT_CHECK)
        if "OK" not in (check or ""):
            raise RuntimeError(
                f"nftables is not available on {node}; "
                "install nftables in the image or enable package install"
            )

    def add_nft_drop_rule(
        self: SupportsExec,
        node: str,
        rule: str,
        *,
        table: str = "filter",
        family: str = "inet",
    ) -> None:
        self._ensure_nft_available(node)
        self._nft_checked(node, f"add table {family} {table}")
        for chain_name in nft_drop_chains(family):
            self._nft_add_chain(node, table, chain_name, family, chain_name)
            self._nft_checked(node, f"add rule {family} {table} {chain_name} {rule}")
        from nika.utils.network_change_log import log_network_change

        log_network_change(
            f"nft drop rule on {node} table={family}/{table}: {rule}",
            mechanism="nft",
            host=node,
            table=table,
            family=family,
            rule=rule,
            action="add_drop_rule",
        )

    def delete_nft_table(
        self: SupportsExec,
        node: str,
        *,
        table: str = "filter",
        family: str = "inet",
    ) -> None:
        self.exec_cmd(node, f"{_NFT_PATH}nft delete table {family} {table}")
        from nika.utils.network_change_log import log_network_change

        log_network_change(
            f"nft delete table on {node}: {family}/{table}",
            mechanism="nft",
            host=node,
            table=table,
            family=family,
            action="delete_table",
        )

    def nft_ruleset_contains(self: SupportsExec, node: str, pattern: str) -> bool:
        return pattern in self.list_nft_ruleset(node)

    def nft_rule_present(
        self: SupportsExec,
        node: str,
        rule: str,
        *,
        table: str = "filter",
        chain: str = "input",
        family: str = "inet",
    ) -> bool:
        """Return whether ``family table chain`` holds exactly ``rule``."""
        listing = self.exec_cmd(node, nft_list_chain_command(table, chain, family))
        return nft_chain_has_rule(listing, rule)

    def nft_drop_rule_present(
        self: SupportsExec,
        node: str,
        rule: str,
        *,
        table: str = "filter",
        family: str = "inet",
    ) -> bool:
        """Return whether ``add_nft_drop_rule`` state is live in every hooked chain."""
        return all(
            self.nft_rule_present(node, rule, table=table, chain=c, family=family)
            for c in nft_drop_chains(family)
        )
