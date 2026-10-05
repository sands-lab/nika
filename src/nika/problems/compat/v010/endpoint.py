"""Endpoint faults retained for the 0.1.0 benchmark cases."""

from pydantic import BaseModel, Field

from nika.problems.base import FailureDomain, ProblemBase, build_verify_result
from nika.problems.rca import node_resource

_SLOW_HTTP_SERVER = """\
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "51200")
        self.end_headers()
        for _ in range(50):
            self.wfile.write(b"x" * 1024)
            self.wfile.flush()
            time.sleep(0.1)

HTTPServer(("0.0.0.0", 80), Handler).serve_forever()
"""


class HostCrashParams(BaseModel):
    host_name: str = Field(description="Host container to pause.")


class HostCrash(ProblemBase):
    failure_domain = FailureDomain.ENDPOINT_APPLICATION
    root_cause_name = "host_crash"
    description = "An endpoint stops responding because its host is unavailable."
    TAGS = ["pc"]
    Params = HostCrashParams

    def root_cause_resources(self, params: HostCrashParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: HostCrashParams):
        self.runtime.pause(params.host_name)

    def verify_fault(self, params: HostCrashParams) -> dict:
        status = self.runtime.node_status(params.host_name)
        return build_verify_result(
            self.root_cause_name,
            status == "paused",
            {"host": params.host_name, "status": status},
        )

    def recover_fault(self, params: HostCrashParams) -> dict:
        self.runtime.unpause(params.host_name)
        status = self.runtime.node_status(params.host_name)
        return build_verify_result(
            self.root_cause_name,
            status == "running",
            {"host": params.host_name, "status": status},
        )


class SenderApplicationDelayParams(BaseModel):
    host_name: str = Field(description="HTTP server host to throttle.")


class SenderApplicationDelay(ProblemBase):
    failure_domain = FailureDomain.ENDPOINT_APPLICATION
    root_cause_name = "sender_application_delay"
    description = "The HTTP sender application deliberately paces its responses."
    TAGS = ["http"]
    Params = SenderApplicationDelayParams

    def root_cause_resources(self, params: SenderApplicationDelayParams):
        return [node_resource(params.host_name)]

    def _slow_running(self, host: str) -> bool:
        return bool(
            self.runtime.exec(
                host, "pgrep -af '[n]ika-010-slow-sender.py' || true"
            ).strip()
        )

    def inject_fault(self, params: SenderApplicationDelayParams):
        self.runtime.write_file(
            params.host_name,
            "/tmp/nika-010-slow-sender.py",
            _SLOW_HTTP_SERVER,
        )
        # campus_lan serves :80 with nginx and runs web_server.service with
        # Restart=always, so both must be stopped before the slow sender binds.
        self.runtime.exec(
            params.host_name,
            "systemctl stop web_server 2>/dev/null; "
            "pkill -f '[w]eb_server.py' || true; "
            "nginx -s stop 2>/dev/null || true; "
            "pkill -x nginx 2>/dev/null || true; "
            "pkill -f '[h]ttp.server' || true; "
            "sleep 0.3; "
            "nohup python3 /tmp/nika-010-slow-sender.py "
            "</dev/null >/tmp/nika-010-slow-sender.log 2>&1 &",
        )

    def verify_fault(self, params: SenderApplicationDelayParams) -> dict:
        configured = self._slow_running(params.host_name)
        return build_verify_result(
            self.root_cause_name,
            configured,
            {"host": params.host_name, "paced": configured},
        )

    def recover_fault(self, params: SenderApplicationDelayParams) -> dict:
        self.runtime.exec(
            params.host_name, "pkill -f '[n]ika-010-slow-sender.py' || true"
        )
        if self.scenario_name == "campus_lan":
            self.runtime.exec(
                params.host_name, "systemctl start web_server 2>/dev/null; nginx"
            )
        else:
            self.runtime.exec(
                params.host_name,
                "cd /var/www && nohup python3 -m http.server 80 "
                "</dev/null >/tmp/nika-010-web-restored.log 2>&1 &",
            )
        configured = self._slow_running(params.host_name)
        restored = "UP" in self.runtime.exec(
            params.host_name,
            "curl -fsS -m 5 http://127.0.0.1/ >/dev/null && echo UP || true",
            timeout=8,
        )
        return build_verify_result(
            self.root_cause_name,
            not configured and restored,
            {"host": params.host_name, "paced": configured, "restored": restored},
        )
