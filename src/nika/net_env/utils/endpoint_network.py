"""Restore declared endpoint routes and neighbors after an attachment outage."""

import shlex


def restore_endpoint_network(runtime, env, node: str) -> None:
    endpoints = set(env.hosts) | {
        server for servers in env.servers.values() for server in servers
    }
    if node not in endpoints or env.lab is None:
        return
    startup = env.lab.fs.readtext(f"{node}.startup")
    for line in startup.splitlines():
        if not line.startswith(("ip route ", "ip neigh ")):
            continue
        tokens = shlex.split(line)
        if len(tokens) < 3 or tokens[0] != "ip":
            continue
        if tokens[1] not in {"route", "neigh"} or tokens[2] not in {"add", "replace"}:
            continue
        tokens[2] = "replace"
        runtime.exec(node, shlex.join(tokens))
