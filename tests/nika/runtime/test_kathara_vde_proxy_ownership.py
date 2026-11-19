"""Ownership checks for VDE helper startup failures."""

from unittest.mock import Mock, patch

import pytest

from nika.runtime.kathara.vde_proxy import (
    KatharaVdeFaultProxy,
    LinkEnd,
    VdeFaultProxyState,
)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("owned", "error"),
    [(False, RuntimeError("name conflict")), (True, KeyboardInterrupt("interrupted"))],
)
def test_failed_proxy_creation_only_removes_its_own_container(owned, error):
    proxy = object.__new__(KatharaVdeFaultProxy)
    proxy.runtime = Mock(lab_name="owned-lab")
    proxy._client = Mock()
    proxy.discover = Mock(return_value=None)
    proxy._link_ends = Mock(return_value=(LinkEnd("a", "eth0"), LinkEnd("b", "eth0")))
    proxy._hub_ctl = Mock(return_value="hub")
    proxy._key = Mock(return_value="key")
    container = proxy._client.containers.get.return_value
    container.labels = {"nika.creation_token": "our-token" if owned else "other-token"}
    proxy._client.containers.run.side_effect = error

    with (
        patch(
            "nika.runtime.kathara.vde_proxy.secrets.token_hex", return_value="our-token"
        ),
        patch("nika.runtime.kathara.vde_proxy.isolated_container_pid", return_value=42),
        pytest.raises(type(error), match=str(error)),
    ):
        proxy.insert("a", "eth0")

    if owned:
        container.remove.assert_called_once_with(force=True)
    else:
        container.remove.assert_not_called()


@pytest.mark.unit
def test_interrupted_splice_removes_the_created_helper():
    proxy = object.__new__(KatharaVdeFaultProxy)
    proxy.runtime = Mock(lab_name="owned-lab")
    proxy._client = Mock()
    proxy._client.containers.run.return_value.id = "helper-id"
    proxy.discover = Mock(return_value=None)
    proxy._link_ends = Mock(return_value=(LinkEnd("a", "eth0"), LinkEnd("b", "eth0")))
    proxy._hub_ctl = Mock(return_value="hub")
    proxy._key = Mock(return_value="key")
    proxy._start_splice = Mock(side_effect=KeyboardInterrupt())
    proxy.remove = Mock()

    with (
        patch("nika.runtime.kathara.vde_proxy.isolated_container_pid", return_value=42),
        pytest.raises(KeyboardInterrupt),
    ):
        proxy.insert("a", "eth0")

    state = proxy.remove.call_args.args[0]
    assert state.proxy_id == "helper-id"
    assert proxy.remove.call_args.kwargs == {}


@pytest.mark.unit
def test_interrupted_splice_reports_cleanup_failure():
    proxy = object.__new__(KatharaVdeFaultProxy)
    proxy.runtime = Mock(lab_name="owned-lab")
    proxy._client = Mock()
    proxy._client.containers.run.return_value.id = "helper-id"
    proxy.discover = Mock(return_value=None)
    proxy._link_ends = Mock(return_value=(LinkEnd("a", "eth0"), LinkEnd("b", "eth0")))
    proxy._hub_ctl = Mock(return_value="hub")
    proxy._key = Mock(return_value="key")
    proxy._start_splice = Mock(side_effect=KeyboardInterrupt())
    proxy.remove = Mock(side_effect=RuntimeError("stop failed"))

    with (
        patch("nika.runtime.kathara.vde_proxy.isolated_container_pid", return_value=42),
        pytest.raises(KeyboardInterrupt) as caught,
    ):
        proxy.insert("a", "eth0")

    assert any(
        "helper-id" in note and "stop failed" in note for note in caught.value.__notes__
    )


@pytest.mark.unit
@pytest.mark.parametrize("suppress_errors", [False, True])
def test_failed_splice_stop_does_not_force_remove_helper(suppress_errors):
    proxy = object.__new__(KatharaVdeFaultProxy)
    proxy._client = Mock()
    proxy.stop_link_flap = Mock()
    proxy._stop_splice = Mock(side_effect=RuntimeError("stop failed"))
    proxy._remove_container = Mock()
    state = VdeFaultProxyState(
        "key", LinkEnd("a", "eth0"), LinkEnd("b", "eth0"), "helper-id"
    )

    if suppress_errors:
        proxy.remove(state, suppress_errors=True)
    else:
        with pytest.raises(RuntimeError, match="stop failed"):
            proxy.remove(state)

    proxy._remove_container.assert_not_called()
