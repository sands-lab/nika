"""Observe a timeline while its benchmark engine owns execution and recovery."""

from nika.benchmark_engine.client import EngineClient
from nika.remote.schedule import RemoteSessionScheduler
from nika.workflows.schedule import _RUNNERS, _RUNNERS_LOCK, write_schedule_owner


class EngineSessionScheduler(RemoteSessionScheduler):
    """The frontend's view of a timeline; the engine holds its schedule."""

    def __init__(self, engine, handle):
        super().__init__(handle.session_id, None, client=engine)
        self.handle = handle

    def _call(self, operation, **kwargs):
        return self.client.schedule_operation(self.handle, operation)

    def _sync(self):
        # Engine containers and the frontend share trial artifacts and SessionStore.
        pass

    def prepare(self):
        # begin() already prepared the timeline on the engine. Native engines
        # register their actual scheduler when it starts; container engines
        # need this observer for the frontend's agent continuation loop.
        write_schedule_owner(self.directory, self.session_id)
        if isinstance(self.client, EngineClient):
            with _RUNNERS_LOCK:
                if self.session_id in _RUNNERS:
                    raise ValueError("This session already has a scheduler")
                _RUNNERS[self.session_id] = self
        self._prepared = True

    def wait_agent_start(self):
        self._call("agent-ready")
