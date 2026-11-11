"""Web server presents an expired TLS certificate."""

from __future__ import annotations

import calendar
import re
import shlex
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, model_validator

from nika.config import pkg_path
from nika.net_env.campus_lan.verify import web_server_dns_name
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

# 2 KiB object that every campus web server's application serves through nginx
# on ports 80 and 443 (web.conf ``location /``), so probes stay cheap on every
# device profile, including 1 Mbit/s RouterOS links.
PROBE_PATH = "/small"

# curl exit code for a peer certificate that fails verification.
_CURLE_PEER_FAILED_VERIFICATION = 60
# OpenSSL X509_V_ERR_CERT_HAS_EXPIRED, as reported by curl's ssl_verify_result.
_X509_V_ERR_CERT_HAS_EXPIRED = 10
_HTTP_OK = "200"
_RELOAD_WAIT_SEC = 10.0
_POLL_SEC = 0.5

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


def local_resolve(url: str) -> str:
    """``--resolve`` entry that sends ``url``'s name and port to 127.0.0.1."""
    parts = urlsplit(url)
    return f"{parts.hostname}:{parts.port or 443}:127.0.0.1"


def served_expiry_cmd(url: str, *, resolve: str | None = None) -> str:
    """Served certificate's validity end and the local clock, without validation."""
    pin = f"--resolve {shlex.quote(resolve)} " if resolve else ""
    return (
        f"curl -vsk -m 10 -o /dev/null {pin}{shlex.quote(url)} 2>&1 "
        "| grep -iE 'subject:|issuer:|expire date:'; echo now=$(date -u +%s)"
    )


def http_code_cmd(url: str) -> str:
    return f"curl -s -m 10 -o /dev/null -w '%{{http_code}}' {shlex.quote(url)} 2>/dev/null; true"


def served_expired(output: str) -> bool:
    """Whether :func:`served_expiry_cmd` shows a validity end before the local clock."""
    not_after = parse_expire_date(output)
    now = _int_after("now", output)
    return not_after is not None and now is not None and not_after < now


def _wait_until(probe: Callable[[], bool], timeout_sec: float) -> bool:
    """Poll ``probe`` until it holds or ``timeout_sec`` passes; probe at least once."""
    deadline = time.monotonic() + timeout_sec
    while True:
        if probe():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_POLL_SEC)


class TlsCertificateExpiredParams(BaseModel):
    """Parameters for an expired certificate on an HTTPS web server.

    URLs left unset point at ``PROBE_PATH`` on the web server's campus DNS name.
    """

    host_name: str = Field(description="HTTPS web server whose certificate expires.")
    client_host: str = Field(description="Client that trusts the campus CA.")
    control_host: str | None = Field(
        default=None,
        description="Another web server with a certificate from the same CA.",
    )
    https_url: str | None = Field(
        default=None, description="HTTPS URL served by host_name."
    )
    http_url: str | None = Field(
        default=None, description="Plain HTTP URL served by host_name."
    )
    control_https_url: str | None = Field(
        default=None, description="HTTPS URL served by control_host."
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

    @model_validator(mode="after")
    def _derive_urls(self) -> TlsCertificateExpiredParams:
        if self.https_url is None or self.http_url is None:
            name = web_server_dns_name(self.host_name)
            self.https_url = self.https_url or f"https://{name}{PROBE_PATH}"
            self.http_url = self.http_url or f"http://{name}{PROBE_PATH}"
        if self.control_https_url is None:
            if self.control_host is None:
                raise ValueError("control_host or control_https_url is required")
            control = web_server_dns_name(self.control_host)
            self.control_https_url = f"https://{control}{PROBE_PATH}"
        return self


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
        webs = list(ctx.servers.get("web") or [])
        host = prefer_named(webs, "web_server_0", "web_server_0")
        others = [w for w in webs if w != host]
        return {
            "host_name": host,
            "client_host": prefer_named(pcs, "pc_1_1_1_1", ctx.host0),
            "control_host": prefer_named(others, "web_server_1", "web_server_1"),
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

    def _served_locally(self, params: TlsCertificateExpiredParams) -> dict[str, Any]:
        """Certificate nginx on host_name serves for https_url, read on host_name."""
        out = self.runtime.exec(
            params.host_name,
            served_expiry_cmd(
                params.https_url, resolve=local_resolve(params.https_url)
            ),
            timeout=20,
        )
        return {
            "not_after": parse_expire_date(out),
            "now": _int_after("now", out),
            "expired": served_expired(out),
        }

    def _https(self, client: str, url: str) -> dict[str, Any]:
        return parse_https_probe(
            self.runtime.exec(client, https_probe_cmd(url), timeout=20)
        )

    def symptom_sample(self, params: TlsCertificateExpiredParams) -> dict[str, Any]:
        """HTTPS, plain HTTP, and the control server from the client; the backend on the server."""
        http_code = self._http_code(params.client_host, params.http_url)
        backend_code = self._http_code(params.host_name, params.backend_url)
        return {
            "https": self._https(params.client_host, params.https_url),
            "http_code": http_code,
            "http_ok": http_code == _HTTP_OK,
            "backend_code": backend_code,
            "backend_ok": backend_code == _HTTP_OK,
            "control": self._https(params.client_host, params.control_https_url),
        }

    def _reload(self, host: str) -> None:
        checked_exec(self.runtime, host, "nginx -t 2>&1 && nginx -s reload")

    def _wait_served_expired(self, params: TlsCertificateExpiredParams) -> bool:
        return _wait_until(
            lambda: self._served_locally(params)["expired"], _RELOAD_WAIT_SEC
        )

    def _wait_https_validates(self, params: TlsCertificateExpiredParams) -> bool:
        return _wait_until(
            lambda: self._https(params.client_host, params.https_url)["validates"],
            _RELOAD_WAIT_SEC,
        )

    def _restore_originals(self, host: str) -> list[str]:
        """Write back both snapshots, attempting each; return the failures."""
        errors = []
        for snapshot in (self._key_snapshot, self._cert_snapshot):
            try:
                snapshot.restore(self.runtime, host)
            except Exception as exc:  # noqa: BLE001 - restore the other file too
                errors.append(f"{snapshot.path}: {exc}")
        return errors

    def _roll_back(self, host: str) -> None:
        """Put the original pair back after a failed injection; never raises."""
        errors = self._restore_originals(host)
        if not errors:
            try:
                self._reload(host)
            except Exception as exc:  # noqa: BLE001 - keep the injection error
                errors.append(f"reload: {exc}")
        if errors:
            system_logger.error(
                "tls_certificate_expired rollback on %s incomplete: %s",
                host,
                "; ".join(errors),
            )

    def inject_fault(self, params: TlsCertificateExpiredParams):
        host = params.host_name
        if not self._nginx_running(host):
            raise RuntimeError(f"nginx is not running on {host}")
        # Restore state stays in this instance; no copies are left on the node.
        # A repeated injection keeps the originals instead of the expired pair.
        if self._cert_snapshot is None or self._key_snapshot is None:
            cert_snapshot = FileSnapshot.capture(self.runtime, host, params.cert_path)
            key_snapshot = FileSnapshot.capture(self.runtime, host, params.key_path)
            self._cert_snapshot, self._key_snapshot = cert_snapshot, key_snapshot
        cert, key = self._expired_pair()
        try:
            # Checked, read-back writes; nginx reloads only once both files match.
            FileSnapshot(params.key_path, key).restore(self.runtime, host)
            FileSnapshot(params.cert_path, cert).restore(self.runtime, host)
            self._reload(host)
            if not self._wait_served_expired(params):
                raise RuntimeError(
                    f"nginx on {host} did not serve the expired certificate "
                    f"within {_RELOAD_WAIT_SEC:g}s"
                )
        except Exception:
            self._roll_back(host)
            raise
        system_logger.info(
            "Injected tls_certificate_expired on %s (%s)", host, params.cert_path
        )

    def verify_fault(self, params: TlsCertificateExpiredParams) -> dict:
        """The expired pair is installed and nginx on the server serves it."""
        host = params.host_name
        expired_cert, expired_key = self._expired_pair()
        cert_installed = FileSnapshot(params.cert_path, expired_cert).matches(
            self.runtime, host
        )
        key_installed = FileSnapshot(params.key_path, expired_key).matches(
            self.runtime, host
        )
        nginx_running = self._nginx_running(host)
        served = self._served_locally(params)
        verified = bool(
            cert_installed and key_installed and nginx_running and served["expired"]
        )
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "host": host,
                "cert_installed": cert_installed,
                "key_installed": key_installed,
                "nginx_running": nginx_running,
                "served_not_after": served["not_after"],
                "server_now": served["now"],
                "served_expired": served["expired"],
            },
        )

    def recover_fault(self, params: TlsCertificateExpiredParams) -> dict:
        host = params.host_name
        if self._cert_snapshot is None or self._key_snapshot is None:
            return build_verify_result(
                fault_type=self.root_cause_name,
                verified=False,
                details={"error": "Pre-injection certificate is unavailable."},
            )
        errors = self._restore_originals(host)
        if errors:
            return build_verify_result(
                fault_type=self.root_cause_name,
                verified=False,
                details={"host": host, "restore_errors": errors},
            )
        self._reload(host)
        https_validated = self._wait_https_validates(params)
        sample = self.symptom_sample(params)
        verified = bool(
            https_validated
            and self._cert_snapshot.matches(self.runtime, host)
            and self._key_snapshot.matches(self.runtime, host)
            and sample["https"]["validates"]
            and sample["http_ok"]
        )
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={"host": host, "https_validated": https_validated, **sample},
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
