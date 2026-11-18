"""Mint the campus_lan lab PKI: the campus CA, CA-signed server certificates, and re-issues.

Lab-only. The CA is trusted only inside NIKA lab containers.
"""

from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def _time(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(dt.UTC)


def _new_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _write_key(key, path: Path) -> None:
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )


def _write_cert(cert: x509.Certificate, path: Path) -> None:
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def _load_key(path: Path):
    return serialization.load_pem_private_key(path.read_bytes(), None)


def _load_cert(path: Path) -> x509.Certificate:
    return x509.load_pem_x509_certificate(path.read_bytes())


def make_ca(args: argparse.Namespace) -> None:
    key = _new_key()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, args.cn)])
    ski = x509.SubjectKeyIdentifier.from_public_key(key.public_key())
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_time(args.not_before))
        .not_valid_after(_time(args.not_after))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(ski, critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ski),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    _write_key(key, Path(args.key))
    _write_cert(cert, Path(args.cert))


def issue_server(args: argparse.Namespace) -> None:
    ca_key, ca_cert = _load_key(Path(args.ca_key)), _load_cert(Path(args.ca_cert))
    key_path = Path(args.key)
    if args.reuse_key:
        key = _load_key(key_path)
    else:
        key = _new_key()
        _write_key(key, key_path)
    sans = [x509.DNSName(d) for d in args.dns] + [
        x509.IPAddress(ipaddress.ip_address(i)) for i in args.ip
    ]
    ca_ski = ca_cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, args.cn)]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_time(args.not_before))
        .not_valid_after(_time(args.not_after))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(
                ca_ski.value
            ),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    _write_cert(cert, Path(args.cert))


def reissue(args: argparse.Namespace) -> None:
    """Copy a certificate with a new serial and the given fields changed, signed by the CA."""
    ca_key, ca_cert = _load_key(Path(args.ca_key)), _load_cert(Path(args.ca_cert))
    src = _load_cert(Path(args.cert))
    if src.issuer != ca_cert.subject:
        raise SystemExit(f"{args.cert} is not issued by {args.ca_cert}")
    builder = (
        x509.CertificateBuilder()
        .subject_name(src.subject)
        .issuer_name(src.issuer)
        .public_key(src.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(
            _time(args.not_before) if args.not_before else src.not_valid_before_utc
        )
        .not_valid_after(
            _time(args.not_after) if args.not_after else src.not_valid_after_utc
        )
    )
    for ext in src.extensions:
        builder = builder.add_extension(ext.value, critical=ext.critical)
    _write_cert(builder.sign(ca_key, hashes.SHA256()), Path(args.out))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(required=True)

    ca = sub.add_parser("ca", help="Create a self-signed CA key and certificate.")
    ca.add_argument("--cn", default="NIKA Campus CA")
    ca.add_argument("--not-before", required=True)
    ca.add_argument("--not-after", required=True)
    ca.add_argument("--key", required=True)
    ca.add_argument("--cert", required=True)
    ca.set_defaults(func=make_ca)

    srv = sub.add_parser("server", help="Issue a CA-signed TLS server certificate.")
    srv.add_argument("--ca-key", required=True)
    srv.add_argument("--ca-cert", required=True)
    srv.add_argument("--cn", required=True)
    srv.add_argument("--dns", action="append", default=[])
    srv.add_argument("--ip", action="append", default=[])
    srv.add_argument("--not-before", required=True)
    srv.add_argument("--not-after", required=True)
    srv.add_argument("--key", required=True, help="Server key to write or reuse.")
    srv.add_argument("--reuse-key", action="store_true")
    srv.add_argument("--cert", required=True)
    srv.set_defaults(func=issue_server)

    re = sub.add_parser("reissue", help="Re-issue a certificate with a new serial.")
    re.add_argument("--ca-key", required=True)
    re.add_argument("--ca-cert", required=True)
    re.add_argument("--cert", required=True, help="Certificate to copy.")
    re.add_argument("--not-before")
    re.add_argument("--not-after")
    re.add_argument("--out", required=True)
    re.set_defaults(func=reissue)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
