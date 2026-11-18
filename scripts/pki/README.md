# Campus lab PKI

> [!WARNING]
> `campus-ca.key` is a lab-only CA key. The CA is trusted only inside NIKA `campus_lan` containers; never trust it or use it anywhere else.

Use `campus_pki.py` to issue a new campus certificate, for example for a TLS failure, without regenerating the whole PKI. Its certificates chain to the campus CA that every `campus_lan` PC trusts. The environment's certificates live in [`src/nika/net_env/campus_lan/`](../../src/nika/net_env/campus_lan/): `campus-ca.crt` (the trust anchor), `campus-repository.crt`/`.key` (the server certificate on `web0`-`web3.local`), and `campus-repository-expired.crt` (the same server certificate, re-issued with a notAfter of 2026-11-01T00:00:00Z).

| Command | What it does |
|---|---|
| `ca` | Creates a new self-signed CA key and certificate. |
| `server` | Issues a CA-signed TLS server certificate with the given names, addresses, and validity. `--reuse-key` keeps an existing key. |
| `reissue` | Copies a CA-signed certificate with a new serial number, changing only the validity bounds you pass. |

This is how the expired server certificate was made:

```bash
cd src/nika/net_env/campus_lan
uv run python ../../../../scripts/pki/campus_pki.py reissue \
  --ca-key ../../../../scripts/pki/campus-ca.key --ca-cert campus-ca.crt \
  --cert campus-repository.crt --not-after 2026-11-01T00:00:00Z \
  --out campus-repository-expired.crt
```
