"""Align scenario workloads to one host CPU set before deployment."""

from __future__ import annotations

import functools
import json
import os
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

CPU_ENV = "NIKA_HEALTHY_CPUSET"


def parse_mask(value: str) -> set[int]:
    result = set()
    for part in value.replace(" ", ",").split(","):
        if not part:
            continue
        bounds = part.split("-")
        first, last = int(bounds[0]), int(bounds[-1])
        result.update(range(first, last + 1))
    return result


def mask_for_count(count: int) -> str:
    available = sorted(os.sched_getaffinity(0))
    if count > len(available):
        raise ValueError(
            f"Scenario needs {count} CPUs; only {len(available)} are available"
        )
    return ",".join(str(cpu) for cpu in available[:count])


def service_usage() -> dict:
    result = {}
    for unit in ("docker.service", "containerd.service", "incus.service"):
        if unit == "incus.service":
            pid = int(
                subprocess.check_output(
                    ["systemctl", "show", unit, "--property=MainPID", "--value"],
                    text=True,
                ).strip()
            )
            if pid:
                usage = process_usage(pid)
                # Incus places QEMU children in its own service cgroup.
                result[unit] = {
                    key: usage[key]
                    for key in (
                        "pid",
                        "user_cpu_ticks",
                        "system_cpu_ticks",
                        "ticks_per_second",
                    )
                }
            continue
        group = subprocess.check_output(
            ["systemctl", "show", unit, "--property=ControlGroup", "--value"], text=True
        ).strip()
        path = Path("/sys/fs/cgroup") / group.lstrip("/") / "cpu.stat"
        if group and path.exists():
            result[unit] = {
                key: int(value)
                for key, value in (
                    line.split() for line in path.read_text().splitlines()
                )
            }
    return result


def process_usage(pid: int) -> dict:
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    result = {
        "pid": pid,
        "user_cpu_ticks": int(fields[11]),
        "system_cpu_ticks": int(fields[12]),
        "ticks_per_second": os.sysconf("SC_CLK_TCK"),
        "rss_bytes": int(fields[21]) * os.sysconf("SC_PAGE_SIZE"),
    }
    group = next(
        line.split(":", 2)[2]
        for line in Path(f"/proc/{pid}/cgroup").read_text().splitlines()
        if line.startswith("0::")
    )
    path = Path("/sys/fs/cgroup") / group.lstrip("/")
    result["cgroup"] = group
    result["cgroup_counters"] = {
        name: (path / name).read_text().strip()
        for name in ("cpu.stat", "memory.current", "hugetlb.2MB.current")
        if (path / name).exists()
    }
    return result


@contextmanager
def incus_spawn_affinity(mask: str):
    pid = int(
        subprocess.check_output(
            ["systemctl", "show", "incus.service", "--property=MainPID", "--value"],
            text=True,
        ).strip()
    )
    affinities = {
        tuple(sorted(os.sched_getaffinity(int(task.name))))
        for task in Path(f"/proc/{pid}/task").iterdir()
    }
    if len(affinities) != 1:
        raise RuntimeError("Incus daemon has custom thread affinities; preserve them")
    original = next(iter(affinities))
    original_mask = ",".join(map(str, original))
    started = time.time()
    record = {"pid": pid, "original": original_mask, "cpus": mask}

    def apply(value: str):
        subprocess.run(
            ["sudo", "-n", "taskset", "-a", "-pc", value, str(pid)],
            capture_output=True,
            check=True,
        )

    try:
        apply(mask)
        thread_affinities(pid, parse_mask(mask))
        yield
    finally:
        apply(original_mask)
        thread_affinities(pid, set(original))
        record.update(
            restored=True,
            started_epoch=started,
            duration_sec=time.time() - started,
        )
        path = Path(os.environ["NIKA_HEALTHY_PHASE_LOG"]).with_name(
            "incus-spawn-affinity.jsonl"
        )
        with path.open("a") as output:
            output.write(json.dumps(record) + "\n")


def install_hooks(mask: str) -> None:
    from docker.api.client import APIClient
    from nika.runtime.containerlab.runtime import ContainerlabRuntime
    import yaml

    original_create = APIClient.create_container_from_config

    @functools.wraps(original_create)
    def create(client, config, *args, **kwargs):
        config.setdefault("HostConfig", {})["CpusetCpus"] = mask
        return original_create(client, config, *args, **kwargs)

    APIClient.create_container_from_config = create
    original_clab = ContainerlabRuntime._run_clab

    @functools.wraps(original_clab)
    def clab(runtime, *args):
        if args and args[0] == "deploy":
            topology = Path(args[args.index("-t") + 1])
            document = yaml.safe_load(topology.read_text())
            for node in document["topology"]["nodes"].values():
                node["cpu-set"] = mask
            topology.write_text(yaml.safe_dump(document, sort_keys=False))
        return original_clab(runtime, *args)

    ContainerlabRuntime._run_clab = clab

    from experiment.incus import golden, trial
    from experiment.incus import incus as api

    original_incus = api.incus

    @functools.wraps(original_incus)
    def incus(*args, **kwargs):
        if args and args[0] == "copy":
            args = tuple(
                "limits.cpu=" + mask if str(arg).startswith("limits.cpu=") else arg
                for arg in args
            )
        if (
            args
            and args[0] == "start"
            and str(args[1]).startswith(golden.GOLDEN_PREFIX)
        ):
            current = original_incus(
                "config", "get", args[1], "limits.cpu", project=kwargs.get("project")
            ).strip()
            if parse_mask(current) != parse_mask(mask):
                raise RuntimeError(
                    f"Incus VM has a different CPU set before start: {current}"
                )
        if args and args[0] == "start":
            # limits.cpu pins vCPUs. The spawn affinity also bounds emulator
            # and I/O threads before QEMU executes, then restores the daemon.
            with incus_spawn_affinity(mask):
                return original_incus(*args, **kwargs)
        return original_incus(*args, **kwargs)

    api.incus = incus
    golden.incus = incus
    trial.incus = incus


@contextmanager
def alignment(mode: str):
    mask = os.environ.get(CPU_ENV)
    if not mask:
        yield
        return
    target = parse_mask(mask)
    original = os.sched_getaffinity(0)
    if not target or not target <= original:
        raise ValueError(
            f"Invalid workload CPU set {mask}; available CPUs are {sorted(original)}"
        )
    from experiment.convergence.collect import _require_idle_host

    _require_idle_host()
    scopes = []
    record = {
        "mode": mode,
        "cpus": sorted(target),
        "worker_original_cpus": sorted(original),
        "shared_services_before": service_usage(),
        "plugin_scopes": scopes,
    }
    path = Path(os.environ["NIKA_HEALTHY_PHASE_LOG"]).with_name(
        f"{mode}-cpu-{os.getpid()}.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.sched_setaffinity(0, target)
        install_hooks(mask)
        if mode == "native":
            listed = subprocess.check_output(
                ["docker", "plugin", "ls", "--format", "{{.Name}}"], text=True
            ).splitlines()
            for name in listed:
                if not name.startswith("kathara/katharanp_vde:"):
                    continue
                plugin = json.loads(
                    subprocess.check_output(
                        ["docker", "plugin", "inspect", name], text=True
                    )
                )[0]
                identifier = plugin["Id"]
                groups = list(
                    Path("/sys/fs/cgroup/system.slice").glob(f"*-{identifier}.scope")
                )
                if len(groups) != 1:
                    raise RuntimeError(
                        f"Cannot identify the Kathara network plugin cgroup: {identifier}"
                    )
                group = groups[0]
                previous = (group / "cpuset.cpus").read_text().strip()
                scopes.append(
                    {
                        "unit": group.name,
                        "original": previous,
                        "cpu_stat_before": (group / "cpu.stat").read_text(),
                    }
                )
                subprocess.run(
                    [
                        "sudo",
                        "-n",
                        "systemctl",
                        "set-property",
                        "--runtime",
                        group.name,
                        "AllowedCPUs=" + mask.replace(",", " "),
                    ],
                    check=True,
                )
                if (
                    parse_mask((group / "cpuset.cpus.effective").read_text().strip())
                    != target
                ):
                    raise RuntimeError(
                        "Kathara network plugin CPU restriction did not take effect"
                    )
        path.write_text(json.dumps(record, indent=2))
        yield
    finally:
        for scope in reversed(scopes):
            scope["cpu_stat_after"] = (
                Path("/sys/fs/cgroup/system.slice") / scope["unit"] / "cpu.stat"
            ).read_text()
            subprocess.run(
                [
                    "sudo",
                    "-n",
                    "systemctl",
                    "set-property",
                    "--runtime",
                    scope["unit"],
                    "AllowedCPUs=" + scope["original"].replace(",", " "),
                ],
                check=True,
            )
        os.sched_setaffinity(0, original)
        record.update(restored=True, shared_services_after=service_usage())
        path.write_text(json.dumps(record, indent=2))


def thread_affinities(pid: int, target: set[int]) -> dict:
    result = {}
    for task in Path(f"/proc/{pid}/task").iterdir():
        try:
            allowed = os.sched_getaffinity(int(task.name))
        except ProcessLookupError:
            continue
        if not allowed <= target:
            raise RuntimeError(
                f"Thread {task.name} can execute outside the scenario CPU set: {sorted(allowed)}"
            )
        result[task.name] = sorted(allowed)
    return result


def audit(session_id: str) -> dict | None:
    mask = os.environ.get(CPU_ENV)
    if not mask:
        return None
    target = parse_mask(mask)
    result = {
        "cpus": sorted(target),
        "worker": sorted(os.sched_getaffinity(0)),
        "worker_usage": process_usage(os.getpid()),
    }
    from nika.firecracker.trial import active_trial_lab
    from experiment.incus.trial import active_trial_lab as active_incus

    if (lab := active_trial_lab()) is not None:
        pid = int((lab.dir / "firecracker.pid").read_text())
        result["vm_threads"] = thread_affinities(pid, target)
        result["vm_usage"] = process_usage(pid)
    elif (lab := active_incus()) is not None:
        from experiment.incus.incus import incus

        state = json.loads(
            incus(
                "query",
                f"/1.0/instances/{lab.golden.name}/state?project={lab.project}",
            )
        )
        result["vm_threads"] = thread_affinities(state["pid"], target)
        result["vm_usage"] = process_usage(state["pid"])
    else:
        from nika.workflows.session.containers import list_session_containers
        from nika.runtime.shared.containers import docker_client

        _, _, rows = list_session_containers(session_id)
        nodes = {}
        for row in rows:
            data = docker_client().api.inspect_container(row["container_id"])
            actual = data["HostConfig"]["CpusetCpus"]
            if parse_mask(actual) != target:
                raise RuntimeError(
                    f"Container {row['name']} was created with CPU set {actual}"
                )
            nodes[row["name"]] = {
                "cpuset": actual,
                "threads": thread_affinities(data["State"]["Pid"], target),
                "usage": process_usage(data["State"]["Pid"]),
            }
        result["containers"] = nodes
    return result
