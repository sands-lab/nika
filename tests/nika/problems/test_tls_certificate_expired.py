from __future__ import annotations

import base64
import datetime as dt
import re
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding
from pydantic import ValidationError

from nika.config import pkg_path
from nika.problems.endpoint_application import tls
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
_SERVER = "web_server_0"
_CLIENT = "pc_1_1_1_1"
_CERT_PATH = "/etc/ssl/certs/campus-repository.crt"
_KEY_PATH = "/etc/ssl/private/campus-repository.key"
_EXPIRED_CERT = (_CAMPUS / "campus-repository-expired.crt").read_text()
_EXPIRED_KEY = (_CAMPUS / "campus-repository-expired.key").read_text()
_MARKER = "NIKA_RECOVERY_COMMAND_OK"
# 2026-11-07T00:00:00Z: after the expired certificate, before the healthy one.
_NOW = 1794009600
_HTTPS = "https://web0.local/small"
_HTTP = "http://web0.local/small"
_CONTROL = "https://web1.local/small"
_BACKEND = "http://127.0.0.1:8000/small"


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    monkeypatch.setattr(tls, "_RELOAD_WAIT_SEC", 0.0)
    monkeypatch.setattr(tls, "_POLL_SEC", 0.0)


class _Runtime:
    """Campus web server and client: files, nginx reloads, and curl outputs."""

    def __init__(self, **overrides: str) -> None:
        self.files = {
            _CERT_PATH: (_CAMPUS / "campus-repository.crt").read_text(),
            _KEY_PATH: (_CAMPUS / "campus-repository.key").read_text(),
        }
        self.overrides = overrides
        self.reloads = 0
        self.apply_reload = True
        self.fail_write: str | None = None
        self.commands: list[tuple[str, str]] = []
        self._served = self.files[_CERT_PATH]

    def _serves_expired(self) -> bool:
        return self._served == _EXPIRED_CERT

    def exec(self, host: str, command: str, timeout: float = 10) -> str:
        # Probes must never pull the 64 MiB catalog.
        assert "catalog" not in command, command
        self.commands.append((host, command))
        for needle, output in self.overrides.items():
            if needle in command:
                return output
        if "base64 -d >" in command:
            assert host == _SERVER
            encoded, path = re.search(
                r"printf %s '?([^' ]+)'? \| base64 -d > (\S+)\)", command
            ).groups()
            if path == self.fail_write:
                self.fail_write = None  # a transient failure: the next write works
                return "base64: write error\n"
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
            assert host == _SERVER
            self.reloads += 1
            if self.apply_reload:
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
                f"now={_NOW}\n"
            )
        if "curl -sS" in command:
            assert host == _CLIENT
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
        host_name=_SERVER, client_host=_CLIENT, control_host="web_server_1"
    )


def _injected() -> tuple[_Runtime, TlsCertificateExpired, TlsCertificateExpiredParams]:
    runtime = _Runtime()
    problem = _problem(runtime)
    params = _params()
    problem.inject_fault(params)
    return runtime, problem, params


def test_registered_for_campus_lan_only() -> None:
    assert get_problem_class("tls_certificate_expired") is TlsCertificateExpired
    assert compatible("tls_certificate_expired", "campus_lan")
    assert not compatible("tls_certificate_expired", "dc_clos")


def test_params_derive_small_object_urls_from_server_names() -> None:
    params = TlsCertificateExpiredParams(
        host_name="web_server_2", client_host=_CLIENT, control_host="web_server_3"
    )
    assert params.https_url == "https://web2.local/small"
    assert params.http_url == "http://web2.local/small"
    assert params.control_https_url == "https://web3.local/small"


def test_params_keep_explicit_urls() -> None:
    params = TlsCertificateExpiredParams(
        host_name="repo",
        client_host=_CLIENT,
        https_url="https://repo.example/x",
        http_url="http://repo.example/x",
        control_https_url="https://other.example/x",
    )
    assert params.https_url == "https://repo.example/x"
    assert params.control_https_url == "https://other.example/x"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"host_name": _SERVER, "client_host": _CLIENT},
        {
            "host_name": "dns_server",
            "client_host": _CLIENT,
            "control_host": "web_server_1",
        },
    ],
    ids=["no control server", "not a campus web server"],
)
def test_params_reject_underivable_urls(kwargs) -> None:
    with pytest.raises(ValidationError):
        TlsCertificateExpiredParams(**kwargs)


def test_benchmark_params_pick_a_different_control_server() -> None:
    ctx = SimpleNamespace(
        host_pool=["pc_1_1_1_1", "pc_2_1_1_1"],
        host0="pc_1_1_1_1",
        servers={"web": ["web_server_0", "web_server_1", "web_server_2"]},
    )
    chosen = TlsCertificateExpired.benchmark_inject_params(ctx)
    assert chosen == {
        "host_name": "web_server_0",
        "client_host": "pc_1_1_1_1",
        "control_host": "web_server_1",
    }
    params = TlsCertificateExpiredParams(**chosen)
    assert (params.https_url, params.control_https_url) == (_HTTPS, _CONTROL)


def test_inject_verify_recover_round_trip() -> None:
    runtime = _Runtime()
    problem = _problem(runtime)
    params = _params()
    original = dict(runtime.files)

    healthy, _ = tls_baseline(problem, params)
    assert healthy

    problem.inject_fault(params)
    assert runtime.files == {_CERT_PATH: _EXPIRED_CERT, _KEY_PATH: _EXPIRED_KEY}
    assert runtime.reloads == 1

    runtime.commands.clear()
    result = problem.verify_fault(params)
    details = result["details"]
    assert result["verified"] is True, details
    assert details["cert_installed"] and details["key_installed"]
    assert (
        details["served_not_after"] == EXPIRED_NOT_AFTER_EPOCH < details["server_now"]
    )
    # Artifact check only: every command runs on the server, none on a client.
    assert {host for host, _ in runtime.commands} == {_SERVER}
    assert any(
        "--resolve web0.local:443:127.0.0.1" in cmd for _, cmd in runtime.commands
    )

    symptom, evidence = tls_expired(problem, params)
    assert symptom, evidence
    after = evidence["details"]["after"]
    assert after["https"]["rc"] == 60
    assert after["https"]["ssl_verify_result"] == 10
    assert after["http_ok"] and after["backend_ok"]
    assert after["control"]["validates"]

    recovered = problem.recover_fault(params)
    assert recovered["verified"] is True, recovered
    assert recovered["details"]["https_validated"] is True
    assert runtime.files == original
    assert runtime.reloads == 2


def _revert(path: str):
    original = {
        _CERT_PATH: (_CAMPUS / "campus-repository.crt").read_text(),
        _KEY_PATH: (_CAMPUS / "campus-repository.key").read_text(),
    }[path]
    return lambda runtime: runtime.files.__setitem__(path, original)


def _override(needle: str, output: str):
    return lambda runtime: runtime.overrides.__setitem__(needle, output)


@pytest.mark.parametrize(
    ("break_one", "reason"),
    [
        (_revert(_CERT_PATH), "certificate file not installed"),
        (_revert(_KEY_PATH), "key file not installed"),
        (_override("pgrep -x nginx", "NO"), "nginx not running"),
        (
            _override(
                "--resolve",
                "*  expire date: Nov  1 00:00:00 2026 GMT\nnow=1793000000\n",
            ),
            "server clock before notAfter",
        ),
        (_override("--resolve", f"now={_NOW}\n"), "no certificate served"),
    ],
)
def test_verify_rejects_each_missing_artifact(break_one, reason) -> None:
    runtime, problem, params = _injected()
    assert problem.verify_fault(params)["verified"] is True
    break_one(runtime)
    assert problem.verify_fault(params)["verified"] is False, reason


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        (
            {f"{_HTTPS} 2>&1; echo rc": "verify=0 code=200\nrc=0\n"},
            "HTTPS still validates",
        ),
        (
            {
                f"{_HTTPS} 2>&1; echo rc": "curl: (60) SSL: no alternative certificate subject name matches\nverify=62 code=0\nrc=60\n"
            },
            "validation fails for another reason",
        ),
        ({f"'%{{http_code}}' {_HTTP}": "503"}, "plain HTTP broken"),
        ({f"'%{{http_code}}' {_BACKEND}": "502"}, "backend application down"),
        (
            {
                f"{_CONTROL} 2>&1; echo rc": "curl: (60) SSL certificate problem\nverify=19 code=0\nrc=60\n"
            },
            "CA trust broken",
        ),
    ],
)
def test_symptom_rejects_any_other_state(overrides, reason) -> None:
    runtime = _Runtime()
    problem = _problem(runtime)
    params = _params()
    assert tls_baseline(problem, params)[0]
    problem.inject_fault(params)
    assert tls_expired(problem, params)[0]
    runtime.overrides = overrides
    assert tls_expired(problem, params)[0] is False, reason


def test_symptom_requires_a_baseline() -> None:
    _, problem, params = _injected()
    assert tls_expired(problem, params) == (False, {"error": "no_healthy_baseline"})


@pytest.mark.parametrize("failing", [_CERT_PATH, _KEY_PATH])
def test_failed_write_rolls_back_both_files(failing) -> None:
    runtime = _Runtime()
    original = dict(runtime.files)
    runtime.fail_write = failing
    problem = _problem(runtime)
    with pytest.raises(RuntimeError):
        problem.inject_fault(_params())
    # The rollback rewrites both originals and reloads nginx with them.
    assert runtime.files == original
    assert runtime.reloads == 1


def test_inject_raises_and_rolls_back_when_never_served() -> None:
    runtime = _Runtime()
    original = dict(runtime.files)
    runtime.apply_reload = False
    with pytest.raises(RuntimeError, match="did not serve the expired certificate"):
        _problem(runtime).inject_fault(_params())
    assert runtime.files == original
    assert runtime.reloads == 2  # injection reload, then rollback reload


def test_repeated_inject_keeps_the_original_snapshots() -> None:
    runtime, problem, params = _injected()
    problem.inject_fault(params)
    assert problem.recover_fault(params)["verified"] is True
    assert runtime.files[_CERT_PATH] == (_CAMPUS / "campus-repository.crt").read_text()
    assert runtime.files[_KEY_PATH] == (_CAMPUS / "campus-repository.key").read_text()


def test_recover_reports_https_that_never_validates() -> None:
    runtime, problem, params = _injected()
    runtime.apply_reload = False
    result = problem.recover_fault(params)
    assert result["verified"] is False
    assert result["details"]["https_validated"] is False


def test_recover_reports_restore_failures() -> None:
    runtime, problem, params = _injected()
    runtime.fail_write = _CERT_PATH
    result = problem.recover_fault(params)
    assert result["verified"] is False
    assert result["details"]["restore_errors"]
    # The key was still restored even though the certificate write failed.
    assert runtime.files[_KEY_PATH] == (_CAMPUS / "campus-repository.key").read_text()


def test_recover_without_inject_reports_failure() -> None:
    assert _problem(_Runtime()).recover_fault(_params())["verified"] is False


def test_oracle_probes_use_the_small_object() -> None:
    playbook = _problem(_Runtime()).oracle_playbook(_params())
    commands = " ".join(step.args.get("command", "") for step in playbook.steps)
    assert "catalog" not in commands
    assert _HTTPS in commands and _HTTP in commands and _CONTROL in commands


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
    leaf = x509.load_pem_x509_certificate(_EXPIRED_CERT.encode())
    key = serialization.load_pem_private_key(_EXPIRED_KEY.encode(), None)

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
