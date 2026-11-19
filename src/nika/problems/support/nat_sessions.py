"""Bounded connection diagnostics against an existing HTTP service."""

from __future__ import annotations

import json
import re
import shlex

TCP_BURST = r"""
import json, shutil, signal, socket, sys, threading, time
target, port, count = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
held, failed, lock = [], [0], threading.Lock()
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
def attempt():
    sock = socket.socket()
    sock.settimeout(2)
    try:
        sock.connect((target, port))
        request = (f"GET /static/pixel?kb=0 HTTP/1.1\r\nHost: {target}\r\n"
                   "Connection: keep-alive\r\n\r\n").encode()
        sock.sendall(request)
        if sock.recv(1) != b"H":
            raise OSError("invalid HTTP response")
        with lock:
            held.append(sock)
    except OSError:
        sock.close()
        with lock:
            failed[0] += 1
threads = [threading.Thread(target=attempt) for _ in range(count)]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join()
try:
    print(json.dumps({"connected": len(held), "failed": failed[0]}), flush=True)
    if len(sys.argv) > 4:
        time.sleep(float(sys.argv[4]))
finally:
    for sock in held:
        sock.close()
    if len(sys.argv) > 5:
        shutil.rmtree(sys.argv[5])
"""


def start_connections(address: str, port: int, count: int) -> str:
    """Hold diagnostic sockets for the following gateway read, at most 45 seconds."""
    script = f"""
import json, pathlib, shutil, subprocess, sys, tempfile, time
directory = pathlib.Path(tempfile.mkdtemp(prefix='nat-check-', dir='/tmp'))
with (directory / 'result').open('w') as output, (directory / 'error').open('w') as error:
    child = subprocess.Popen([sys.executable, '-c', {TCP_BURST!r}, {address!r}, {str(port)!r}, {str(count)!r}, '45', str(directory)], stdin=subprocess.DEVNULL, stdout=output, stderr=error, start_new_session=True)
try:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            result = json.loads((directory / 'result').read_text())
        except (OSError, ValueError):
            if child.poll() is not None:
                raise RuntimeError('connection diagnostic exited before readiness')
            time.sleep(0.1)
            continue
        print(json.dumps(dict(result, pid=child.pid, directory=str(directory))))
        break
    else:
        raise TimeoutError('connection diagnostic readiness timed out')
except BaseException:
    child.terminate()
    child.wait(timeout=5)
    shutil.rmtree(directory, ignore_errors=True)
    raise
"""
    return "python3 -c " + shlex.quote(script)


def stop_connections(observation: str) -> str:
    """Stop only the process identified by this diagnostic's private directory."""
    record = json.loads(observation)
    directory, pid = record["directory"], int(record["pid"])
    if not re.fullmatch(r"/tmp/nat-check-[a-zA-Z0-9_]+", directory) or pid < 2:
        raise ValueError("invalid connection diagnostic identity")
    script = f"""
import json, os, pathlib, signal, time
directory = pathlib.Path({directory!r})
process = pathlib.Path('/proc/{pid}')
if directory.exists() and process.exists():
    arguments = (process / 'cmdline').read_bytes().split(b'\\0')
    if str(directory).encode() not in arguments:
        raise RuntimeError('connection diagnostic process identity changed')
    os.kill({pid}, signal.SIGTERM)
deadline = time.monotonic() + 5
while directory.exists() and time.monotonic() < deadline:
    time.sleep(0.1)
print(json.dumps({{'stopped': not directory.exists()}}))
"""
    return "python3 -c " + shlex.quote(script)
