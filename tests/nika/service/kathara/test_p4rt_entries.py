"""P4Runtime table encoding follows the pipeline's declared match types."""

import pytest

pytestmark = pytest.mark.unit
pytest.importorskip("p4.v1.p4runtime_pb2")

from p4.config.v1 import p4info_pb2  # noqa: E402
from p4.v1 import p4runtime_pb2  # noqa: E402
from nika.net_env.utils.kathara import p4rt_manager as manager  # noqa: E402


@pytest.fixture
def index():
    info = p4info_pb2.P4Info()
    table = info.tables.add()
    table.preamble.name, table.preamble.id = "Ingress.selection", 0x2000001
    for ident, (name, kind, width) in enumerate(
        [
            ("srcAddr", "TERNARY", 32),
            ("dstAddr", "LPM", 32),
            ("protocol", "OPTIONAL", 8),
            ("srcPort", "RANGE", 16),
            ("dstPort", "EXACT", 16),
        ],
        1,
    ):
        field = table.match_fields.add()
        field.id, field.name, field.bitwidth = ident, name, width
        field.match_type = getattr(p4info_pb2.MatchField, kind)
    action = info.actions.add()
    action.preamble.name, action.preamble.id = "Ingress.select", 0x1000001
    param = action.params.add()
    param.id, param.name, param.bitwidth = 1, "port", 9
    return manager.P4InfoIndex(info)


def test_entry_encodes_all_match_types_and_action(index):
    update = manager.table_entity(
        index,
        "selection",
        match={
            "srcAddr": "10.1.2.3/24",
            "dstAddr": "192.0.2.9/24",
            "protocol": "6",
            "srcPort": "1000..2000",
            "dstPort": "80",
        },
        action="select",
        params={"port": "0x101"},
        priority=10,
    )
    entry = update.entity.table_entry
    assert update.type == p4runtime_pb2.Update.INSERT
    assert entry.priority == 10
    assert entry.match[0].ternary.value == b"\x0a\x01\x02\x00"
    assert entry.match[0].ternary.mask == b"\xff\xff\xff\x00"
    assert entry.match[1].lpm.value == b"\xc0\x00\x02\x00"
    assert entry.match[1].lpm.prefix_len == 24
    assert entry.match[2].optional.value == b"\x06"
    assert int.from_bytes(entry.match[3].range.high) == 2000
    assert entry.match[4].exact.value == b"\x00\x50"
    assert entry.action.action.params[0].value == b"\x01\x01"


def test_wildcards_are_omitted_and_ternary_values_are_masked(index):
    entry = manager.table_entity(
        index,
        "selection",
        match={
            "srcAddr": "10.1.2.3&&&255.255.0.0",
            "dstAddr": "0.0.0.0/0",
            "srcPort": "0..65535",
        },
    ).entity.table_entry
    assert len(entry.match) == 1
    assert entry.match[0].ternary.value == b"\x0a\x01\x00\x00"
    assert not manager.table_entity(
        index, "selection", match={"srcAddr": "0&&&0"}
    ).entity.table_entry.match


@pytest.mark.parametrize("verb", ["insert", "modify", "delete"])
def test_write_parser_and_delete_key(index, verb):
    args = manager.build_parser().parse_args(
        [
            verb,
            "--switch",
            "leaf_1",
            "--table",
            "selection",
            "--match",
            "srcAddr=10.1.2.3/32",
            "--priority",
            "12",
            "--action",
            "select",
            "--param",
            "port=3",
        ]
    )
    update = manager.table_entity(
        index,
        args.table,
        match=manager.key_values(args.match),
        action=args.action,
        params=manager.key_values(args.param),
        kind=verb.upper(),
        priority=args.priority,
    )
    assert update.type == getattr(p4runtime_pb2.Update, verb.upper())
    assert update.entity.table_entry.HasField("action") == (verb != "delete")
    assert update.entity.table_entry.priority == 12


@pytest.mark.parametrize("value", ["protocol=256", "dstPort=-1", "srcPort=2000..1000"])
def test_invalid_match_values_fail_before_device_write(index, value):
    with pytest.raises((ValueError, OverflowError)):
        manager.table_entity(index, "selection", match=manager.key_values([value]))


def test_duplicate_arguments_are_not_silently_overwritten():
    with pytest.raises(ValueError, match="unique"):
        manager.key_values(["port=1", "port=2"])


def test_indirect_entries_and_default_actions(index):
    assert (
        manager.table_entity(
            index, "selection", group_id=7
        ).entity.table_entry.action.action_profile_group_id
        == 7
    )
    assert (
        manager.table_entity(
            index, "selection", member_id=4
        ).entity.table_entry.action.action_profile_member_id
        == 4
    )
    entry = manager.table_entity(
        index,
        "selection",
        default=True,
        kind="MODIFY",
        action="select",
        params={"port": 2},
    ).entity.table_entry
    assert entry.is_default_action and not entry.match


def test_connections_use_fresh_elections_for_arbitration_pipeline_and_writes(
    monkeypatch,
):
    from unittest.mock import MagicMock

    clock = iter([100, 101])
    monkeypatch.setattr(manager.time, "time_ns", lambda: next(clock))
    monkeypatch.setattr(manager.grpc, "insecure_channel", lambda address: MagicMock())
    monkeypatch.setattr(
        manager.p4runtime_pb2_grpc, "P4RuntimeStub", lambda channel: MagicMock()
    )
    monkeypatch.setattr(manager.SwitchClient, "_listen", lambda self: None)
    arbitrate = manager.SwitchClient._become_master
    with monkeypatch.context() as context:
        context.setattr(manager.SwitchClient, "_become_master", lambda self: None)
        clients = [manager.SwitchClient("switch:9559", 1) for _ in range(2)]
    try:
        assert clients[0].election_id < clients[1].election_id
        for client in clients:
            client._arbitration.set()
            arbitrate(client)
            request = client._requests.get_nowait()
            assert request.arbitration.election_id.low == client.election_id
            client.set_pipeline(p4info_pb2.P4Info(), b"pipeline")
            request = client.stub.SetForwardingPipelineConfig.call_args.args[0]
            assert request.election_id.low == client.election_id
            client.write([], p4runtime_pb2.WriteRequest.CONTINUE_ON_ERROR)
            assert (
                client.stub.Write.call_args.args[0].election_id.low
                == client.election_id
            )
    finally:
        for client in clients:
            client.close()


def test_help_and_argument_errors_use_the_public_tool_name(capsys):
    parser = manager.build_parser()
    assert parser.format_help().startswith("usage: p4rt_exec ")
    assert "p4rt_manager.py" not in parser.format_help()
    with pytest.raises(SystemExit, match="2"):
        parser.parse_args(["probe"])
    error = capsys.readouterr().err
    assert "p4rt_exec probe" in error
    assert "--switch" in error
    assert "p4rt_manager.py" not in error


@pytest.mark.parametrize("verb", ["read", "read-table", "counters"])
def test_unavailable_switch_reads_return_rpc_status_without_traceback(
    monkeypatch, capsys, verb
):
    import json

    class Unavailable(manager.grpc.RpcError):
        def code(self):
            return manager.grpc.StatusCode.UNAVAILABLE

        def details(self):
            return "connection refused"

    def unavailable(_args):
        raise Unavailable()

    monkeypatch.setattr(manager, "cmd_" + verb.replace("-", "_"), unavailable)
    argv = [verb, "--switch", "gateway_1"]
    if verb == "read-table":
        argv += ["--table", "ipv4_lpm"]
    with pytest.raises(SystemExit, match="1"):
        manager.main(argv)
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "ok": False,
        "command": verb,
        "switch": "gateway_1",
        "status": "UNAVAILABLE",
        "error": "connection refused",
    }
    assert captured.err == ""


def test_unexpected_manager_errors_remain_errors(monkeypatch):
    def broken(_args):
        raise ValueError("malformed pipeline")

    monkeypatch.setattr(manager, "cmd_read", broken)
    with pytest.raises(ValueError, match="malformed pipeline"):
        manager.main(["read", "--switch", "gateway_1"])


@pytest.mark.parametrize("verb", ["read", "read-table", "counters"])
def test_read_commands_query_without_acquiring_mastership(monkeypatch, verb):
    from unittest.mock import MagicMock

    switch = {"address": "switch:9559", "device_id": 1}
    monkeypatch.setattr(
        manager, "load_json", lambda path: {"switches": {"leaf_1": switch}}
    )
    monkeypatch.setattr(manager, "load_p4info", lambda path: p4info_pb2.P4Info())
    index = MagicMock()
    index.tables = {"ipv4_lpm": object()}
    monkeypatch.setattr(manager, "P4InfoIndex", lambda info: index)
    stub = MagicMock()
    monkeypatch.setattr(manager.grpc, "insecure_channel", lambda address: MagicMock())
    monkeypatch.setattr(
        manager.p4runtime_pb2_grpc, "P4RuntimeStub", lambda channel: stub
    )
    observed = {
        "pipeline": {},
        "counters": {},
        "members": [],
        "groups": [],
        "ipv4_lpm": [],
    }
    monkeypatch.setattr(manager, "observed_switch", lambda client, index: observed)
    monkeypatch.setattr(manager, "table_rows", lambda client, index, table: [])
    args = [verb, "--switch", "leaf_1"]
    if verb == "read-table":
        args += ["--table", "ipv4_lpm"]
    getattr(manager, "cmd_" + verb.replace("-", "_"))(
        manager.build_parser().parse_args(args)
    )
    stub.StreamChannel.assert_not_called()


def test_query_connection_cannot_write_or_replace_pipeline(monkeypatch):
    from unittest.mock import MagicMock

    stub = MagicMock()
    monkeypatch.setattr(manager.grpc, "insecure_channel", lambda address: MagicMock())
    monkeypatch.setattr(
        manager.p4runtime_pb2_grpc, "P4RuntimeStub", lambda channel: stub
    )
    client = manager.SwitchClient("switch:9559", 1, primary=False)
    try:
        client.get_pipeline()
        client.read_entities()
        stub.GetForwardingPipelineConfig.assert_called_once()
        stub.Read.assert_called_once()
        stub.StreamChannel.assert_not_called()
        with pytest.raises(RuntimeError, match="primary connection"):
            client.write([], p4runtime_pb2.WriteRequest.CONTINUE_ON_ERROR)
        with pytest.raises(RuntimeError, match="primary connection"):
            client.set_pipeline(p4info_pb2.P4Info(), b"pipeline")
        stub.Write.assert_not_called()
        stub.SetForwardingPipelineConfig.assert_not_called()
    finally:
        client.close()
