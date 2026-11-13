"""Stage the idle archive batch on an environment's endpoints."""

from nika.config import pkg_path
from nika.runtime.base import LabRuntime
from nika.utils.parallel import bounded_parallel_map


def prepare_archive_jobs(runtime: LabRuntime, hosts: list[str]) -> None:
    """Install the batch, its default configuration, and its idle data."""
    source = pkg_path("net_env/utils/kathara/archive_job.py").read_text()

    def prepare(host: str) -> None:
        runtime.write_file(host, "/usr/local/bin/archive-job.py", source)
        runtime.write_file(host, "/etc/archive-job.json", '{"workers": 1}\n')
        runtime.exec(host, "python3 /usr/local/bin/archive-job.py prepare", timeout=60)

    bounded_parallel_map(prepare, hosts)
