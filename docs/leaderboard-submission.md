# Leaderboard submission

Submit an official release run to [`sands-lab/nika-leaderboard`](https://github.com/sands-lab/nika-leaderboard).

**Prerequisites:** finished `nika benchmark run --release …`, and authenticated [`gh`](https://cli.github.com/) (`gh auth login` or `GH_TOKEN`, `repo` scope). No manual fork/clone/commit.

## Steps

```shell
nika benchmark run --release 0.1.0 --result_dir results/my-run -a <agent> -m <model>

nika leaderboard template -o results/my-run/submission
# edit metadata.yaml + README.md

nika leaderboard pack --result_dir results/my-run \
  --submission results/my-run/submission

nika leaderboard validate results/my-run/YYYYMMDD_slug \
  --source-result-dir results/my-run

nika leaderboard submit results/my-run/YYYYMMDD_slug
# optional: --draft --title "..." --body "..." --repo sands-lab/nika-leaderboard
```

`pack` writes `{result_dir}/{YYYYMMDD}_{slug}/` (slug from `metadata.info.name`; override with `--out`).  
`submit` validates (unless `--skip-validate`), pushes to `submissions/<release_version>/{YYYYMMDD}_{slug}/`, and opens a PR. CI re-validates packages under `submissions/`.

## Package layout

```text
{YYYYMMDD}_{slug}/
  README.md
  metadata.yaml
  files.json
  results/
    identity.yaml
    metrics.json
    trials/{trial_id}/result.json
```

Remote path: `submissions/<release_version>/{YYYYMMDD}_{slug}/`.  
Traces and per-case run artifacts are not included; integrity uses `source_run_sha256` and the frozen `benchmark.digest`.

## `metadata.yaml`

```yaml
info:
  name: ""            # required; display name + folder slug
  authors: ""         # required
  org: null
  site: null
  report: null
  logo: null
  email: null
  github: null
agent:
  model: ""           # required
  framework: ""       # required
  tools: []
  skills: []
  optimization_methods: []
  tags: []
  os_model: false
  os_system: false
  extra: {}
```

## `README.md`

Short system description, authors, and links to code / report / site (if any).

## Validation

- Schema `1`; identity matches the in-tree frozen release
- Exact trial coverage (`case_count × n_trials`); metrics match recomputed aggregates (failures count as 0)
- Package hashes in `files.json`; with `--source-result-dir`, `run.json` matches `source_run_sha256`
- No secrets or absolute paths in package text

## PR checklist

- [ ] Official release run; local `nika leaderboard validate` passed
- [ ] Required metadata fields filled; README describes the system
- [ ] Path is `submissions/<release_version>/{YYYYMMDD}_{slug}/`
- [ ] CI validate workflow is green
