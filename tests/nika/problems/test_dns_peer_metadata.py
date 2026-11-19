"""Resolver edits preserve peer data and avoid a single fresh config ctime."""

import os
import shlex
import subprocess
from types import SimpleNamespace

import pytest

from nika.problems.addressing_neighbor_naming.host_configuration import (
    HostIncorrectDNS,
    HostIncorrectDNSParams,
)
from nika.problems.support.dhcp import DNS_RENEWAL_HOOK

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("with_hook", [False, True])
def test_incorrect_dns_refreshes_only_declared_matching_peers(tmp_path, with_hook):
    nodes = ["client_a", "client_b", "server"]
    files = {}
    for node in nodes:
        directory = tmp_path / node
        directory.mkdir()
        path = directory / "resolv.conf"
        path.write_text("search example.test\nnameserver 192.0.2.53\n")
        os.utime(path, ns=(1700000000123456789, 1700000000987654321))
        files[node] = path
        if with_hook:
            (directory / "nameservers").write_text("# Local resolver options\n")

    class Runtime:
        def exec(self, node, command, **kwargs):
            command = command.replace(
                DNS_RENEWAL_HOOK, shlex.quote(str(tmp_path / node / "nameservers"))
            )
            command = command.replace("/etc/resolv.conf", shlex.quote(str(files[node])))
            return subprocess.run(
                command, shell=True, capture_output=True, text=True, check=True
            ).stdout

        def write_file(self, node, path, content):
            if path == DNS_RENEWAL_HOOK:
                (tmp_path / node / "nameservers").write_text(content)
            else:
                assert path == "/etc/resolv.conf"
                files[node].write_text(content)

    identity = SimpleNamespace(role="host", capabilities={"linux"})
    problem = object.__new__(HostIncorrectDNS)
    problem.runtime = Runtime()
    problem.net_env = SimpleNamespace(
        machine_identities={
            "client_a": identity,
            "client_b": identity,
            "server": SimpleNamespace(role="host", capabilities={"linux", "dns"}),
        }
    )
    before = {node: path.stat() for node, path in files.items()}
    problem.inject_fault(
        HostIncorrectDNSParams(host_name="client_a", fake_dns_ip="192.0.2.54")
    )
    after = {node: path.stat() for node, path in files.items()}
    assert files["client_a"].read_text() == (
        "search example.test\nnameserver 192.0.2.54\n"
    )
    if with_hook:
        assert (tmp_path / "client_a/nameservers").read_text() == (
            '# Local resolver options\n\nnew_domain_name_servers="192.0.2.54"\n'
        )
        assert (
            tmp_path / "client_b/nameservers"
        ).read_text() == "# Local resolver options\n"
    for node in nodes:
        assert after[node].st_mtime_ns == before[node].st_mtime_ns
    assert after["client_b"].st_atime_ns == before["client_b"].st_atime_ns
    assert after["client_b"].st_ctime_ns > before["client_b"].st_ctime_ns
    assert after["server"].st_ctime_ns == before["server"].st_ctime_ns
    assert files["client_b"].read_bytes() == files["server"].read_bytes()


class HookRuntime:
    def __init__(self, root):
        self.root = root

    def exec(self, node, command, **kwargs):
        command = command.replace(
            "/etc/dhcp/dhclient-enter-hooks.d", shlex.quote(str(self.root / node))
        )
        return subprocess.run(
            command, shell=True, capture_output=True, text=True, check=True
        ).stdout

    def write_file(self, node, path, content):
        (self.root / node / path.rsplit("/", 1)[-1]).write_text(content)


def test_neutral_address_hook_is_present_before_any_fault(tmp_path):
    from nika.problems.support.dhcp import prepare_dhcp_renewal_hooks

    node = tmp_path / "client"
    node.mkdir()
    resolver = node / "nameservers"
    resolver.write_text("# Existing resolver configuration\n")
    os.utime(resolver, ns=(1700000000123456789, 1700000000987654321))
    original = resolver.stat()
    runtime = HookRuntime(tmp_path)
    prepare_dhcp_renewal_hooks(runtime, "client", 1700000010)
    assert (node / "address").read_text() == "# Local IPv4 options\n"
    assert resolver.read_text() == "# Existing resolver configuration\n"
    assert resolver.stat().st_mtime_ns == original.st_mtime_ns
    assert (node / "address").stat().st_mtime_ns == 1700000010 * 10**9
    prepare_dhcp_renewal_hooks(runtime, "absent", 1700000010)
    assert not (tmp_path / "absent").exists()


def test_address_override_preserves_peer_files_and_metadata(tmp_path):
    from nika.problems.addressing_neighbor_naming.host_configuration import (
        _override_address_renewal,
    )

    nodes = ["client_a", "client_b", "server"]
    for node in nodes:
        directory = tmp_path / node
        directory.mkdir()
        path = directory / "address"
        path.write_text("# Local IPv4 options\n")
        os.utime(path, ns=(1700000000123456789, 1700000000987654321))
    runtime = HookRuntime(tmp_path)
    identity = SimpleNamespace(role="host", capabilities={"linux"})
    problem = SimpleNamespace(
        runtime=runtime,
        net_env=SimpleNamespace(
            machine_identities={
                "client_a": identity,
                "client_b": identity,
                "server": SimpleNamespace(role="server", capabilities={"linux"}),
            }
        ),
    )
    before = {node: (tmp_path / node / "address").stat() for node in nodes}
    snapshot = _override_address_renewal(
        problem, "client_a", new_subnet_mask="255.0.0.0"
    )
    after = {node: (tmp_path / node / "address").stat() for node in nodes}
    assert snapshot.content == "# Local IPv4 options\n"
    assert 'new_subnet_mask="255.0.0.0"' in (tmp_path / "client_a/address").read_text()
    assert all(after[node].st_mtime_ns == before[node].st_mtime_ns for node in nodes)
    assert after["client_b"].st_atime_ns == before["client_b"].st_atime_ns
    assert after["client_b"].st_ctime_ns > before["client_b"].st_ctime_ns
    assert after["server"].st_ctime_ns == before["server"].st_ctime_ns
    assert (tmp_path / "client_b/address").read_bytes() == (
        tmp_path / "server/address"
    ).read_bytes()


def test_zone_metadata_refresh_preserves_assets_and_matches_dns_peers(tmp_path):
    from nika.problems.addressing_neighbor_naming.dns import DNSRecordError

    identities = {
        "dns_a": SimpleNamespace(role="service", capabilities={"linux", "dns"}),
        "dns_b": SimpleNamespace(role="service", capabilities={"linux", "dns"}),
        "web": SimpleNamespace(role="service", capabilities={"linux", "http"}),
        "host": SimpleNamespace(role="host", capabilities={"linux", "dns"}),
    }
    files = {}
    for node in identities:
        directory = tmp_path / node / "bind"
        directory.mkdir(parents=True)
        for name in ["named.conf", "db." + node]:
            path = directory / name
            path.write_text(node + ":" + name)
            os.utime(path, ns=(1700000000123456789, 1700000000987654321))
            files[node, name] = path
        outside = tmp_path / node / "operator-file"
        outside.write_text("operator data")
        files[node, "operator-file"] = outside
        (directory / "operator-link").symlink_to(outside)

    class Runtime:
        def exec(self, node, command, **kwargs):
            command = command.replace(
                "/etc/bind", shlex.quote(str(tmp_path / node / "bind"))
            )
            return subprocess.run(
                command, shell=True, capture_output=True, text=True, check=True
            ).stdout

    before = {key: path.stat() for key, path in files.items()}
    problem = object.__new__(DNSRecordError)
    problem.runtime = Runtime()
    problem.net_env = SimpleNamespace(machine_identities=identities)
    problem._refresh_zone_metadata("dns_a")
    after = {key: path.stat() for key, path in files.items()}
    for (node, name), path in files.items():
        old, new = before[node, name], after[node, name]
        assert new.st_mtime_ns == old.st_mtime_ns
        assert new.st_atime_ns == old.st_atime_ns
        assert new.st_mode == old.st_mode
        if node in {"dns_a", "dns_b"} and name != "operator-file":
            assert new.st_ctime_ns > old.st_ctime_ns
        else:
            assert new.st_ctime_ns == old.st_ctime_ns
        assert path.read_text() == (
            "operator data" if name == "operator-file" else node + ":" + name
        )
