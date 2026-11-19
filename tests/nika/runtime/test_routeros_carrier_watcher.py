"""The virtual NOS sees each short physical-carrier interval."""

import os
import shutil
import signal
import socketserver
import subprocess
import threading
import time

import pytest

from nika.net_env.utils.routeros.common import _LINK_WATCHER, LINK_MONITOR_PORT

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(not shutil.which("socat"), reason="socat required"),
]


def test_watcher_propagates_each_one_second_carrier_interval(tmp_path):
    commands = []
    condition = threading.Condition()

    class Monitor(socketserver.StreamRequestHandler):
        def handle(self):
            command = self.rfile.readline().decode().strip()
            with condition:
                commands.append(command)
                condition.notify_all()
            # A QEMU monitor remains open after a command, rather than closing.
            time.sleep(2)

    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = False

    server = Server(("127.0.0.1", 0), Monitor)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = tmp_path / "net"
    state = root / "eth1" / "operstate"
    state.parent.mkdir(parents=True)
    state.write_text("up\n")
    script = tmp_path / "watcher"
    script.write_text(
        _LINK_WATCHER.replace("/sys/class/net/", str(root) + "/").replace(
            str(LINK_MONITOR_PORT), str(server.server_address[1])
        )
    )
    process = subprocess.Popen(["bash", str(script), "eth1:1"], start_new_session=True)
    try:
        with condition:
            assert condition.wait_for(lambda: bool(commands), 3), commands
        for link in ("off", "on", "off", "on", "off", "on"):
            started = time.monotonic()
            temporary = state.with_suffix(".next")
            temporary.write_text("down\n" if link == "off" else "up\n")
            with condition:
                before = len(commands)
                temporary.replace(state)
                assert condition.wait_for(
                    lambda: (
                        len(commands) > before
                        and commands[-1] == f"set_link p01 {link}"
                    ),
                    0.8,
                ), commands
            time.sleep(max(0, 1 - (time.monotonic() - started)))
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=3)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert commands == [
        "set_link p01 on",
        "set_link p01 off",
        "set_link p01 on",
        "set_link p01 off",
        "set_link p01 on",
        "set_link p01 off",
        "set_link p01 on",
    ]
