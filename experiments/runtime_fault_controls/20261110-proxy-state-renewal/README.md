# Runtime fault controls, 2026-11-10

These scripts retain the local DHCP, VDE forwarding, and vendor saved-file controls on nika01. The development branch keeps the runtime repairs and durable regression tests. All experiment scripts belong to the single `experiment` branch.

## Evidence and code versions

Raw receipts and validation logs remain at `nika01:/workspace/nika-retained-records/20261110-proxy-state-renewal/`. No cloud upload or Git push was performed. Scripts retain the original session identifiers; create an owned session and update those identifiers before replaying a session-bound control. Use NIKA lifecycle commands to close sessions you create.

| Control | Receipt | Result |
| --- | --- | --- |
| Incorrect address, pre-fix | `dhcp-reproduce.json` | Explicit DHCP renewal restored the healthy address |
| Incorrect address, repaired | `dhcp-fixed.json`, `new-e2e.log` | Natural and explicit renewal retained the wrong address; recovery restored the prior hook, address, routes, renewal, and connectivity |
| Direct VDE link, pre-fix | `proxy-direct-before.jsonl` | Native forwarding was about 40 Mbit/s; the sham proxy exceeded 340 Mbit/s |
| Direct VDE link, repaired | `proxy-direct-final.jsonl` | Healthy, sham, reverse impaired, and recovered forwarding remained about 41 to 43 Mbit/s |
| Relay cancellation | `final-proxy-e2e.log`, `final-proxy-recheck.log`, `proxy-final-cleanup.log` | The first forced-kill check exposed an exited process-group leader cleanup error; the final checks recovered the native hub and removed the owned native child and sockets |
| SR Linux saved state | `srl-metadata.jsonl`, `srl-fault-persistence.jsonl` | Peer-wide refresh aligned modification times and retained the actual disabled interface before recovery |
| Vendor negative controls | `vendor-controls-final.jsonl` | RouterOS and IOS XR boot-source files retained their hashes and times during a real native configuration mutation |

Local repairs: `3c67492d` (DHCP), `517ac26e` (native VDE forwarding), `052b2942` (SR Linux saved files), and `2cc26c66` (cleanup and audit evidence). The matched throughput runs preceded the final forced-exit cleanup refinement; dedicated end-to-end checks validated that refinement. The initial vendor-control fixture used an adapter without RouterOS methods and failed before changing configuration; the final fixture used the existing RouterOS API.

## Scope

The controls do not explain the historical first-snapshot packet loss in issue #321. That work belongs to `fix/273-272-303-321-local` in `../normal-kiwi`; this round did not change its files or repeat its VM experiments. Native commit history remains operational evidence. The generic saved-file timestamp audit lead remains open, including untested files and platforms. The runtime behavior changes need a new benchmark revision; published releases remain unchanged.
