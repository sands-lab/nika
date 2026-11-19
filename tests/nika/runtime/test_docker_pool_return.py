"""Docker connection disposal preserves completed results and request failures."""

import pytest
from urllib3.connection import HTTPConnection
from urllib3.connectionpool import HTTPConnectionPool
from urllib3.exceptions import FullPoolError

from nika.runtime.shared.containers import patch_closed_pool_return

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def original_pool_return(monkeypatch):
    original = HTTPConnectionPool._put_conn
    while getattr(original, "_nika_closed_pool_return", False):
        original = original.__wrapped__
    monkeypatch.setattr(HTTPConnectionPool, "_put_conn", original)


def closing_connection(pool, *, callback_error=False):
    class Connection(HTTPConnection):
        close_calls = 0

        def close(self):
            self.close_calls += 1
            super().close()
            # Deterministically place eviction between discard and size logging.
            pool.close()
            if callback_error:
                absent = None
                absent.qsize()

    return Connection("localhost")


def test_evicted_full_pool_discards_connection_without_overriding_result():
    pool = HTTPConnectionPool("localhost", maxsize=1)
    connection = closing_connection(pool)
    patch_closed_pool_return()

    pool._put_conn(connection)

    assert pool.pool is None
    assert connection.is_closed
    assert connection.close_calls == 1


def test_blocking_pool_capacity_error_is_preserved():
    pool = HTTPConnectionPool("localhost", maxsize=1, block=True)
    connection = closing_connection(pool)
    patch_closed_pool_return()

    with pytest.raises(FullPoolError):
        pool._put_conn(connection)


def test_same_attribute_error_from_connection_callback_is_preserved():
    pool = HTTPConnectionPool("localhost", maxsize=1)
    connection = closing_connection(pool, callback_error=True)
    patch_closed_pool_return()

    with pytest.raises(AttributeError, match="qsize"):
        pool._put_conn(connection)


def test_request_failure_survives_disposal_race():
    pool = HTTPConnectionPool("localhost", maxsize=1)
    connection = closing_connection(pool)
    patch_closed_pool_return()

    with pytest.raises(RuntimeError, match="request failed"):
        try:
            raise RuntimeError("request failed")
        finally:
            pool._put_conn(connection)


def test_repeated_backend_initialization_keeps_one_wrapper():
    patch_closed_pool_return()
    first = HTTPConnectionPool._put_conn

    patch_closed_pool_return()

    assert HTTPConnectionPool._put_conn is first
