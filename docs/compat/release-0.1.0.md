# Run release 0.1.0

Use this page to rerun `nika-bench@0.1.0` with current NIKA, for example to
reproduce results reported against 0.1.0. For new results, run release
[0.2.0](../../benchmark/releases/0.2.0/README.md).

The cases live in [`benchmark/releases/0.1.0/`](../../benchmark/releases/0.1.0/).
Each split (`dev` and `test`) has 56 cases and runs three trials per case by
default.

## Run the release

1. Check the release. `nika benchmark releases` checks only the latest release
   unless you pass `--all`:

   ```shell
   uv run nika benchmark releases --all
   ```

   The 0.1.0 line reports `OK` when every scenario, failure, MCP server, and
   required image resolves.

2. List the cases in a split:

   ```shell
   uv run nika benchmark list --release 0.1.0 --split test
   ```

3. Run the split:

   ```shell
   uv run nika benchmark run --release 0.1.0 --split test \
     --result_dir results/0.1.0-test
   ```

   Each trial writes its run metadata, ground truth, submission, and scores to
   `results/0.1.0-test/trials/<trial>/`.

`nika@0.1` and `nika-bench@0.1.0` also select this release.

## Known limitations

NIKA runs the 0.1.0 cases as published and does not re-audit them. Release
0.1.0 predates the current case review, so some cases inject a fault with weak
or no observable symptoms, and some expose hints to the root cause. Scores on
those cases measure the 0.1.0 task, flaws included.

Fault-presence rechecks still run. NIKA logs each `fault_presence_recheck`
event in the trial's `nika.jsonl` and keeps the trial even when a recheck
misses the fault.

## Where each case runs

Most cases run on a current scenario. When no current scenario can reproduce a
case, the case runs on the original 0.1.0 lab:

- Restored labs: [`src/nika/net_env/compat/v010/`](../../src/nika/net_env/compat/v010/)
- 0.1.0-only failures: [`src/nika/problems/compat/v010/`](../../src/nika/problems/compat/v010/)

| Scenario in the case file | Cases across both splits | Lab |
| --- | ---: | --- |
| `campus_lan` | 38 | Current |
| `dc_clos` | 48 | Current |
| `sdn_l3_clos` | 2 | Current |
| `p4_bloom_filter` | 7 | Original 0.1.0 lab |
| `p4_counter` | 5 | Original 0.1.0 lab |
| `sdn_star` | 5 | Original 0.1.0 lab (POX controller) |
| `sdn_clos` | 3 | Original 0.1.0 lab (POX controller) |
| `p4_mpls` | 2 | Original 0.1.0 lab |
| `rip_small_internet_vpn` | 2 | Original 0.1.0 lab (WireGuard overlay) |

NIKA resolves the original labs and their failures only by explicit ID.
`nika env list`, the failure registry, candidate generation, and release 0.2.0
never include them.

## Failures with a 0.1.0 implementation

Some current failure IDs have a separate 0.1.0 implementation. NIKA uses it
only on the original lab:

| Failure | Original lab | 0.1.0 behavior |
| --- | --- | --- |
| SDN controller, southbound port, and flow rule failures | `sdn_clos`, `sdn_star` | Act on POX and Open vSwitch instead of ONOS. |
| P4 table entry failures | `p4_bloom_filter`, `p4_counter` | Edit tables with `simple_switch_CLI`. |
| `host_vpn_membership_missing` | `rip_small_internet_vpn` | Comments out the host's peer entry on the VPN server. On other scenarios, the ID resolves to `wireguard_peer_key_misconfiguration`. |

In a 0.1.0 session, the agent's submission candidates also include the
0.1.0-only failure IDs.

## Changes from the published rows

The case files keep the original order, seeds, and split sizes. Rows that now
run on a current scenario record the executed scenario, targets, and ground
truth. [Fixed rows](../../benchmark/releases/0.1.0/README.md#fixed-rows) lists
each change and its reason. Git history keeps the published rows.

## Compare results

Compare 0.1.0 runs only when they share the same case files and NIKA commit.
The `nika_git_commit` field in each trial's `run.json` records the code that
injected and scored the trial. Scores from before the fixed rows come from
different executable cases, so don't compare them with current runs.

## Split isolation

Preflight applies the original 0.1.0 split rule: `dev` and `test` share no
exact case. Release 0.2.0 also requires each split to use different deployment
contexts. Release 0.1.0 can't meet that rule, because two P4 failures use
different switches of the same fixed topology in `dev` and `test`.
