"""Web server presents an expired TLS certificate."""

from __future__ import annotations

import calendar
import re
import shlex
import time
from typing import Any

from pydantic import BaseModel, Field

from nika.config import pkg_path
from nika.problems.base import FailureDomain, ProblemBase, build_verify_result
from nika.problems.rca import node_resource
from nika.problems.support.benchmark_targets import prefer_named
from nika.problems.support.oracle import (
    Check,
    OraclePlaybook,
    contains,
    process_running,
    shell,
)
from nika.problems.support.recovery import FileSnapshot, checked_exec
from nika.utils.logger import system_logger

# Leaf for the campus repository names (same SAN as the healthy certificate),
# signed by the campus CA that every campus client trusts, valid
# 2026-10-04T11:24:26Z .. 2026-11-01T00:00:00Z. Static so every run serves the
# same certificate; no clock skew or certificate generation on the node.
_EXPIRED_CERT = "net_env/campus_lan/campus-repository-expired.crt"
_EXPIRED_KEY = "net_env/campus_lan/campus-repository-expired.key"
EXPIRED_NOT_AFTER_EPOCH = 1793491200  # 2026-11-01T00:00:00Z

# curl exit code for a peer certificate that fails verification.
_CURLE_PEER_FAILED_VERIFICATION = 60
# OpenSSL X509_V_ERR_CERT_HAS_EXPIRED, as reported by curl's ssl_verify_result.
_X509_V_ERR_CERT_HAS_EXPIRED = 10
_HTTP_OK = "200"
_RELOAD_WAIT_SEC = 10.0

_EXPIRE_LINE = re.compile(r"expire date:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_EXPIRE_FORMATS = ("%b %d %H:%M:%S %Y %Z", "%a, %d %b %Y %H:%M:%S %Z")


def parse_expire_date(output: str) -> int | None:
    """Epoch seconds of the first ``expire date:`` line in ``curl -v`` output."""
    match = _EXPIRE_LINE.search(output or "")
    if not match:
        return None
    text = " ".join(match.group(1).split())
    for fmt in _EXPIRE_FORMATS:
        try:
            return calendar.timegm(time.strptime(text, fmt))
        except ValueError:
            continue
    return None


def _int_after(label: str, output: str) -> int | None:
    match = re.search(rf"(?:^|\s){label}=(\d+)", output or "")
    return int(match.group(1)) if match else None


def parse_https_probe(output: str) -> dict[str, Any]:
    """Exit code, verify result, and HTTP status of :func:`https_probe_cmd`."""
    rc = _int_after("rc", output)
    verify = _int_after("verify", output)
    code = _int_after("code", output)
    lowered = (output or "").lower()
    return {
        "rc": rc,
        "ssl_verify_result": verify,
        "http_code": code,
        "validates": rc == 0 and verify == 0 and code == 200,
        "expired": rc == _CURLE_PEER_FAILED_VERIFICATION
        and (
            verify == _X509_V_ERR_CERT_HAS_EXPIRED
            or "certificate has expired" in lowered
        ),
    }


def https_probe_cmd(url: str) -> str:
    """HTTPS request that validates the peer against the system trust store."""
    return (
        f"curl -sS -m 10 -o /dev/null "
        f"-w 'verify=%{{ssl_verify_result}} code=%{{http_code}}\\n' {shlex.quote(url)} "
        "2>&1; echo rc=$?"
    )


def served_expiry_cmd(url: str) -> str:
    """Served certificate's validity end and the client clock, without validation."""
    return (
        f"curl -vsk -m 10 -o /dev/null {shlex.quote(url)} 2>&1 "
        "| grep -iE 'subject:|issuer:|expire date:'; echo now=$(date -u +%s)"
    )


def http_code_cmd(url: str) -> str:
    return f"curl -s -m 10 -o /dev/null -w '%{{http_code}}' {shlex.quote(url)} 2>/dev/null; true"


def served_expired(output: str) -> bool:
    """Whether :func:`served_expiry_cmd` shows a validity end before the client clock."""
    not_after = parse_expire_date(output)
    now = _int_after("now", output)
    return not_after is not None and now is not None and not_after < now


class TlsCertificateExpiredParams(BaseModel):
    """Parameters for an expired certificate on an HTTPS web server."""

    host_name: str = Field(description="HTTPS web server whose certificate expires.")
    client_host: str = Field(description="Client that trusts the campus CA.")
    https_url: str = Field(
        default="https://web0.local/packages/catalog.txt",
        description="HTTPS URL served by host_name.",
    )
    http_url: str = Field(
        default="http://web0.local/packages/catalog.txt",
        description="Plain HTTP URL served by host_name.",
    )
    control_https_url: str = Field(
        default="https://web1.local/packages/catalog.txt",
        description="HTTPS URL on another server with a certificate from the same CA.",
    )
    backend_url: str = Field(
        default="http://127.0.0.1:8000/small",
        description="Application behind the TLS front end, probed on host_name.",
    )
    cert_path: str = Field(
        default="/etc/ssl/certs/campus-repository.crt",
        description="Certificate file the web server presents.",
    )
    key_path: str = Field(
        default="/etc/ssl/private/campus-repository.key",
        description="Private key file for cert_path.",
    )


class TlsCertificateExpired(ProblemBase):
    failure_domain = FailureDomain.ENDPOINT_APPLICATION
    symptom_probe = "custom"
    symptom_class = "isolation"
    root_cause_name: str = "tls_certificate_expired"
    description = "Web server presents an expired TLS certificate."
    symptom_desc = "HTTPS clients reject a web server's certificate."
    TAGS: list[str] = ["web", "http"]
    # The only scenario whose clients trust a CA that signs its web servers.
    COMPATIBLE_COLUMNS = frozenset({"campus_lan"})
    Params = TlsCertificateExpiredParams
    BENCHMARK_TARGETS = "canonical"

    @classmethod
    def benchmark_inject_params(cls, ctx):
        pcs = [h for h in ctx.host_pool if h.startswith("pc_")] or list(ctx.host_pool)
        return {
            "host_name": prefer_named(
                list(ctx.servers.get("web") or []), "web_server_0", "web_server_0"
            ),
            "client_host": prefer_named(pcs, "pc_1_1_1_1", ctx.host0),
            "https_url": "https://web0.local/packages/catalog.txt",
            "http_url": "http://web0.local/packages/catalog.txt",
            "control_https_url": "https://web1.local/packages/catalog.txt",
        }

    def __init__(self, scenario_name: str | None = None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self._cert_snapshot: FileSnapshot | None = None
        self._key_snapshot: FileSnapshot | None = None

    def root_cause_resources(self, params: TlsCertificateExpiredParams):
        return [node_resource(params.host_name)]

    @staticmethod
    def _expired_pair() -> tuple[str, str]:
        return (
            pkg_path(_EXPIRED_CERT).read_text(),
            pkg_path(_EXPIRED_KEY).read_text(),
        )

    def _nginx_running(self, host: str) -> bool:
        out = self.runtime.exec(
            host, "pgrep -x nginx >/dev/null 2>&1 && echo OK || echo NO", timeout=10
        )
        return out.strip().endswith("OK")

    def _http_code(self, host: str, url: str) -> str:
        return self.runtime.exec(host, http_code_cmd(url), timeout=20).strip()

    def _served_expiry(self, params: TlsCertificateExpiredParams) -> dict[str, Any]:
        out = self.runtime.exec(
            params.client_host, served_expiry_cmd(params.https_url), timeout=20
        )
        return {
            "not_after": parse_expire_date(out),
            "client_now": _int_after("now", out),
            "expired": served_expired(out),
        }

    def _https(self, client: str, url: str) -> dict[str, Any]:
        return parse_https_probe(
            self.runtime.exec(client, https_probe_cmd(url), timeout=20)
        )

    def client_sample(self, params: TlsCertificateExpiredParams) -> dict[str, Any]:
        """What a campus client sees on HTTPS, plain HTTP, and the control server."""
        http_code = self._http_code(params.client_host, params.http_url)
        return {
            "https": self._https(params.client_host, params.https_url),
            "http_code": http_code,
            "http_ok": http_code == _HTTP_OK,
            "control": self._https(params.client_host, params.control_https_url),
        }

    def _reload(self, host: str) -> None:
        checked_exec(self.runtime, host, "nginx -t 2>&1 && nginx -s reload")

    def _wait_served(self, params, *, expired: bool) -> None:
        deadline = time.monotonic() + _RELOAD_WAIT_SEC
        while time.monotonic() < deadline:
            if self._served_expiry(params)["expired"] is expired:
                return
            time.sleep(0.5)

    def inject_fault(self, params: TlsCertificateExpiredParams):
        if not self._nginx_running(params.host_name):
            raise RuntimeError(f"nginx is not running on {params.host_name}")
        # Restore state stays in this instance; no copies are left on the node.
        self._cert_snapshot = FileSnapshot.capture(
            self.runtime, params.host_name, params.cert_path
        )
        self._key_snapshot = FileSnapshot.capture(
            self.runtime, params.host_name, params.key_path
        )
        cert, key = self._expired_pair()
        self.runtime.write_file(params.host_name, params.key_path, key)
        self.runtime.write_file(params.host_name, params.cert_path, cert)
        self._reload(params.host_name)
        self._wait_served(params, expired=True)
        system_logger.info(
            "Injected tls_certificate_expired on %s (%s)",
            params.host_name,
            params.cert_path,
        )

    def verify_fault(self, params: TlsCertificateExpiredParams) -> dict:
        """The expired certificate is installed and served; only HTTPS fails."""
        expired_cert, _ = self._expired_pair()
        installed = FileSnapshot.capture(
            self.runtime, params.host_name, params.cert_path
        )
        cert_installed = installed.content == expired_cert
        nginx_running = self._nginx_running(params.host_name)
        served = self._served_expiry(params)
        sample = self.client_sample(params)
        backend_code = self._http_code(params.host_name, params.backend_url)
        backend_ok = backend_code == _HTTP_OK
        verified = bool(
            cert_installed
            and nginx_running
            and served["expired"]
            and sample["https"]["expired"]
            and sample["http_ok"]
            and backend_ok
            and sample["control"]["validates"]
        )
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "host": params.host_name,
                "client_host": params.client_host,
                "cert_installed": cert_installed,
                "nginx_running": nginx_running,
                "served_not_after": served["not_after"],
                "client_now": served["client_now"],
                "served_expired": served["expired"],
                "https": sample["https"],
                "http_code": sample["http_code"],
                "http_ok": sample["http_ok"],
                "backend_code": backend_code,
                "backend_ok": backend_ok,
                "control": sample["control"],
            },
        )

    def recover_fault(self, params: TlsCertificateExpiredParams) -> dict:
        cert_snapshot, key_snapshot = self._cert_snapshot, self._key_snapshot
        if cert_snapshot is None or key_snapshot is None:
            return build_verify_result(
                fault_type=self.root_cause_name,
                verified=False,
                details={"error": "Pre-injection certificate is unavailable."},
            )
        key_snapshot.restore(self.runtime, params.host_name)
        cert_snapshot.restore(self.runtime, params.host_name)
        self._reload(params.host_name)
        self._wait_served(params, expired=False)
        sample = self.client_sample(params)
        verified = bool(
            cert_snapshot.matches(self.runtime, params.host_name)
            and key_snapshot.matches(self.runtime, params.host_name)
            and sample["https"]["validates"]
            and sample["http_ok"]
        )
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={"host": params.host_name, **sample},
        )

    def oracle_playbook(self, params: TlsCertificateExpiredParams) -> OraclePlaybook:
        client, server = params.client_host, params.host_name
        return OraclePlaybook(
            finding=(
                f"{server} presents a certificate past its validity end; clients "
                "reject HTTPS while plain HTTP and other servers work."
            ),
            steps=[
                shell(
                    client,
                    https_probe_cmd(params.https_url),
                    f"Reproduce the user symptom: fetch {params.https_url} from {client}.",
                    contains(
                        r"certificate has expired|verify=10\b",
                        "certificate validation fails because it expired",
                    ),
                    evidence=True,
                ),
                shell(
                    client,
                    served_expiry_cmd(params.https_url),
                    "Read the served certificate's validity end and the client clock.",
                    Check(
                        "the served certificate ended before the client clock",
                        served_expired,
                    ),
                    evidence=True,
                ),
                shell(
                    client,
                    http_code_cmd(params.http_url),
                    "Plain HTTP to the same server still answers.",
                    contains(r"^200$", "HTTP returns 200"),
                    evidence=True,
                ),
                shell(
                    client,
                    https_probe_cmd(params.control_https_url),
                    "Another server with a certificate from the same CA validates.",
                    contains(r"verify=0 code=200", "the CA is still trusted"),
                    evidence=True,
                ),
                process_running(server, "nginx", f"The web server on {server} runs."),
            ],
        )
