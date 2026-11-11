from __future__ import annotations

import base64
import datetime as dt
import re

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding

from nika.config import pkg_path
from nika.problems.endpoint_application.tls import (
    EXPIRED_NOT_AFTER_EPOCH,
    TlsCertificateExpired,
    TlsCertificateExpiredParams,
    parse_expire_date,
    parse_https_probe,
)
from nika.problems.registry import compatible, get_problem_class
from nika.validation.symptom.tls_probes import tls_baseline, tls_expired

pytestmark = pytest.mark.unit

_CAMPUS = pkg_path("net_env/campus_lan")
_CERT_PATH = "/etc/ssl/certs/campus-repository.crt"
_KEY_PATH = "/etc/ssl/private/campus-repository.key"
_MARKER = "NIKA_RECOVERY_COMMAND_OK"
# 2026-11-07T00:00:00Z: after the expired certificate, before the healthy one.
_CLIENT_NOW = 1794009600


class _Runtime:
    """Campus web server and client: files, nginx reloads, and curl outputs."""

    def __init__(self, **overrides: str) -> None:
        self.files = {
            _CERT_PATH: (_CAMPUS / "campus-repository.crt").read_text(),
            _KEY_PATH: (_CAMPUS / "campus-repository.key").read_text(),
        }
        self.overrides = overrides
        self.reloads = 0
        self._served = self.files[_CERT_PATH]

    def write_file(self, host: str, path: str, content: str) -> None:
        assert host == "web_server_0"
        self.files[path] = content

    def _serves_expired(self) -> bool:
        return self._served == (_CAMPUS / "campus-repository-expired.crt").read_text()

    def exec(self, host: str, command: str, timeout: float = 10) -> str:
        for needle, output in self.overrides.items():
            if needle in command:
                return output
        if "base64 -d >" in command:
            encoded, path = re.search(
                r"printf %s '?([^' ]+)'? \| base64 -d > (\S+)\)", command
            ).groups()
            self.files[path] = base64.b64decode(encoded).decode()
            return f"\n{_MARKER}\n"
        if "test -e" in command:
            path = re.search(r"test -e (\S+);", command).group(1)
            content = self.files.get(path)
            if content is None:
                return f"ABSENT\n{_MARKER}\n"
            encoded = base64.b64encode(content.encode()).decode()
            return f"PRESENT\n{encoded}\n{_MARKER}\n"
        if "nginx -s reload" in command:
            assert host == "web_server_0"
            self.reloads += 1
            self._served = self.files[_CERT_PATH]
            return f"nginx: configuration file test is successful\n{_MARKER}\n"
        if "pgrep -x nginx" in command:
            return "OK"
        if "curl -vsk" in command:
            expire = (
                "Nov  1 00:00:00 2026 GMT"
                if self._serves_expired()
                else "Sep 10 11:24:26 2126 GMT"
            )
            return (
                "*  subject: CN=web0.local\n"
                f"*  expire date: {expire}\n"
                "*  issuer: CN=campus-repository\n"
                f"now={_CLIENT_NOW}\n"
            )
        if "curl -sS" in command:
            if "web0.local" in command and self._serves_expired():
                return (
                    "curl: (60) SSL certificate problem: certificate has expired\n"
                    "verify=10 code=0\nrc=60\n"
                )
            return "verify=0 code=200\nrc=0\n"
        if "curl -s -m 10" in command:
            return "200"
        raise AssertionError(f"unexpected command on {host}: {command}")


def _problem(runtime: _Runtime) -> TlsCertificateExpired:
    problem = TlsCertificateExpired(None)
    problem.runtime = runtime
    return problem


def _params() -> TlsCertificateExpiredParams:
    return TlsCertificateExpiredParams(
        host_name="web_server_0", client_host="pc_1_1_1_1"
    )


def test_registered_for_campus_lan_only() -> None:
    assert get_problem_class("tls_certificate_expired") is TlsCertificateExpired
    assert compatible("tls_certificate_expired", "campus_lan")
    assert not compatible("tls_certificate_expired", "dc_clos")


def test_inject_verify_recover_round_trip() -> None:
    runtime = _Runtime()
    problem = _problem(runtime)
    params = _params()
    original = dict(runtime.files)

    healthy, _ = tls_baseline(problem, params)
    assert healthy

    problem.inject_fault(params)
    assert (
        runtime.files[_CERT_PATH]
        == (_CAMPUS / "campus-repository-expired.crt").read_text()
    )
    assert (
        runtime.files[_KEY_PATH]
        == (_CAMPUS / "campus-repository-expired.key").read_text()
    )
    assert runtime.reloads == 1

    result = problem.verify_fault(params)
    details = result["details"]
    assert result["verified"] is True, details
    assert (
        details["served_not_after"] == EXPIRED_NOT_AFTER_EPOCH < details["client_now"]
    )
    assert details["https"]["rc"] == 60
    assert details["https"]["ssl_verify_result"] == 10
    assert details["http_ok"] and details["backend_ok"]
    assert details["control"]["validates"]

    symptom, _ = tls_expired(problem, params)
    assert symptom

    recovered = problem.recover_fault(params)
    assert recovered["verified"] is True, recovered
    assert runtime.files == original
    assert runtime.reloads == 2


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        (
            {"curl -vsk": "*  expire date: Nov  1 00:00:00 2026 GMT\nnow=1793000000\n"},
            "client clock before notAfter",
        ),
        ({"curl -vsk": "now=1794009600\n"}, "no certificate presented"),
        (
            {
                "https://web0.local/packages/catalog.txt 2>&1; echo rc": "verify=0 code=200\nrc=0\n"
            },
            "HTTPS still validates",
        ),
        (
            {
                "https://web0.local/packages/catalog.txt 2>&1; echo rc": "curl: (60) SSL: no alternative certificate subject name matches\nverify=62 code=0\nrc=60\n"
            },
            "validation fails for another reason",
        ),
        ({"http://web0.local/packages/catalog.txt": "503"}, "plain HTTP broken"),
        ({"http://127.0.0.1:8000/small": "502"}, "backend application down"),
        (
            {
                "https://web1.local": "curl: (60) SSL certificate problem\nverify=19 code=0\nrc=60\n"
            },
            "CA trust broken",
        ),
        ({"pgrep -x nginx": "NO"}, "nginx not running"),
    ],
)
def test_verify_rejects_any_other_state(overrides, reason) -> None:
    runtime = _Runtime()
    problem = _problem(runtime)
    params = _params()
    problem.inject_fault(params)
    runtime.overrides = overrides
    assert problem.verify_fault(params)["verified"] is False, reason


def test_verify_requires_the_expired_certificate_installed() -> None:
    result = _problem(_Runtime()).verify_fault(_params())
    assert result["verified"] is False
    assert result["details"]["cert_installed"] is False


def test_recover_without_inject_reports_failure() -> None:
    assert _problem(_Runtime()).recover_fault(_params())["verified"] is False


def test_parse_expire_date_formats() -> None:
    assert (
        parse_expire_date("*  expire date: Nov  1 00:00:00 2026 GMT")
        == EXPIRED_NOT_AFTER_EPOCH
    )
    assert (
        parse_expire_date("*  expire date: Sun, 01 Nov 2026 00:00:00 GMT")
        == EXPIRED_NOT_AFTER_EPOCH
    )
    assert parse_expire_date("no certificate") is None
    assert parse_expire_date("*  expire date: garbage") is None


def test_parse_https_probe() -> None:
    expired = parse_https_probe(
        "curl: (60) SSL certificate problem: certificate has expired\nverify=10 code=0\nrc=60"
    )
    assert expired["expired"] and not expired["validates"]
    ok = parse_https_probe("verify=0 code=200\nrc=0")
    assert ok["validates"] and not ok["expired"]
    mismatch = parse_https_probe("verify=62 code=0\nrc=60")
    assert not mismatch["expired"] and not mismatch["validates"]
    assert parse_https_probe("")["rc"] is None


def test_committed_expired_certificate_chains_to_the_campus_ca() -> None:
    ca = x509.load_pem_x509_certificate(
        (_CAMPUS / "campus-repository.crt").read_bytes()
    )
    leaf = x509.load_pem_x509_certificate(
        (_CAMPUS / "campus-repository-expired.crt").read_bytes()
    )
    key = serialization.load_pem_private_key(
        (_CAMPUS / "campus-repository-expired.key").read_bytes(), None
    )

    assert leaf.issuer == ca.subject
    ca.public_key().verify(
        leaf.signature,
        leaf.tbs_certificate_bytes,
        padding.PKCS1v15(),
        leaf.signature_hash_algorithm,
    )
    assert key.public_key().public_numbers() == leaf.public_key().public_numbers()
    san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert "web0.local" in san.get_values_for_type(x509.DNSName)

    now = dt.datetime.now(dt.timezone.utc)
    assert int(leaf.not_valid_after_utc.timestamp()) == EXPIRED_NOT_AFTER_EPOCH
    assert leaf.not_valid_after_utc < now
    assert ca.not_valid_before_utc <= now < ca.not_valid_after_utc
