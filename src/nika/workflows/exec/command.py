"""Execute one shell command inside a host of the selected running session."""

from nika.runtime.factory import resolve_backend
from nika.service.lab.host_api import create_host_api
from nika.utils.session import Session


def exec_command_in_host(
    *,
    host: str,
    command: str,
    session_id: str | None = None,
    timeout: float = 10.0,
) -> str:
    """Run ``command`` on ``host`` within the lab bound to ``session_id``."""
    session = Session()
    session.load_running_session(session_id=session_id)
    session_meta = {k: v for k, v in session.__dict__.items() if k != "store"}
    api = create_host_api(
        lab_name=session.lab_name,
        backend=resolve_backend(session_meta),
        session_meta=session_meta,
    )
    return api.exec_cmd(host_name=host, command=command, timeout=timeout)
