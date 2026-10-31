# Run release 0.1.0

Rerun `nika-bench@0.1.0` from current NIKA to reproduce results against that release. For new results, run [0.2.0](../../benchmark/releases/0.2.0/README.md).

The tasks are in [`benchmark/releases/0.1.0/`](../../benchmark/releases/0.1.0/). Each split (`dev` and `test`) has 56 tasks and three trials per task by default.

## Run the release

```shell
uv run nika benchmark run --release 0.1.0 --split test \
  --result_dir results/0.1.0-test
```

`nika@0.1` and `nika-bench@0.1.0` also select this release. List the tasks with `nika benchmark list --release 0.1.0 --split test`. Check published releases with `nika benchmark releases --all`.

Current NIKA reads the 0.1.0 YAML for listing, packing, and validation. `run --release 0.1.0` checks out the `orchestrator` commit in [`RELEASE.yaml`](../../benchmark/releases/0.1.0/RELEASE.yaml) under `.nika_cache/orchestrators/` and executes that tree. Relative `--result_dir` stays in the current repository. The checkout does not load current `config/nika.yaml`.

Each trial writes run metadata, ground truth, submission, and scores to `results/0.1.0-test/trials/<trial>/`.

## Submit to the leaderboard

After the run, pack and submit from current NIKA. Leaderboard CI installs current NIKA and checks the package against the 0.1.0 YAML. Each split needs 56 tasks × 3 trials = 168 finished slots. See [Leaderboard submission](../benchmarks/leaderboard-submission.md).

```shell
uv run nika leaderboard submit --result_dir results/0.1.0-test \
  --name "My Agent" --authors "Your Name"
```

A `--task-id` subset does not cover the split, so pack rejects it.

## Known limitations

NIKA runs the 0.1.0 tasks as published and does not re-audit them. Some tasks inject a fault with weak or no observable symptoms, and some expose hints to the root cause. Scores measure the 0.1.0 task, including those flaws.

Fault-presence rechecks still run. NIKA logs each `fault_presence_recheck` event in the trial's `nika.jsonl` and keeps the trial even when a recheck misses the fault.

## Where each task runs

The pinned checkout maps most 0.1.0 tasks onto environments that still exist in current NIKA. When no current environment can reproduce a task, that checkout runs the original 0.1.0 environment. Current NIKA does not register those original environments.

| Scenario in the case file | Tasks across both splits | Environment |
| --- | ---: | --- |
| `campus_lan` | 38 | Current |
| `dc_clos` | 48 | Current |
| `sdn_l3_clos` | 2 | Current |
| `p4_bloom_filter` | 7 | Original 0.1.0 environment |
| `p4_counter` | 5 | Original 0.1.0 environment |
| `sdn_star` | 5 | Original 0.1.0 environment (POX controller) |
| `sdn_clos` | 3 | Original 0.1.0 environment (POX controller) |
| `p4_mpls` | 2 | Original 0.1.0 environment |
| `rip_small_internet_vpn` | 2 | Original 0.1.0 environment (WireGuard overlay) |

Compare 0.1.0 runs only when they share the same case files and orchestrator commit. Each trial's `run.json` records `nika_git_commit` from the checkout that injected and scored the trial. [Fixed rows](../../benchmark/releases/0.1.0/README.md#fixed-rows) lists rows that now name a current environment.
