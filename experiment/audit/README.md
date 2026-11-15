# Environment and fault admission audit

Check that a benchmark task starts healthy, shows its declared fault through the agent's tools, and preserves that effect during diagnosis. This runner deploys real Docker-backed labs. It is separate from the trajectory audit in [submission_audit](../submission_audit/README.md).

## Run a selected audit

Prepare NIKA's native lab dependencies and images using the [installation guide](../../docs/operations/installation.md). From the repository root:

```sh
uv run python -m experiment.audit.matrix --help
NIKA_AUDIT_RESULTS_DIR=/workspace/nika-audit-runs/new-run \
  uv run python -m experiment.audit.matrix --scenario dc_clos --jobs 1 --repeats 1
```

`matrix.py` currently selects the release inventory returned by `coverage.release_cases()`, whose default is `0.2.0`. It has no release-selection CLI option. Update and validate the inventory binding before using this historical runner for a new release.

Use `--fault` or `--resource-class` to narrow the selection. `--retry-failed` selects failed or stale records; `--force` reruns selected cases. Uncommitted source requires `--allow-dirty`; those records do not count toward a release report.

The runner admits work using its resource-class slots, available memory and host load. Start with one worker while validating an unfamiliar inventory. Each audit closes its own session.

## Read the results

The runner writes a JSON record for each case and repeat under `NIKA_AUDIT_RESULTS_DIR`. Records include the audit stages, elapsed time, diagnostics and source provenance. Passing records must match the current source and task identity; a missing audit is `not_run`, rather than a pass. The process exits unsuccessfully when any selected case lacks the required current passing repeats.

| Module | Responsibility |
|---|---|
| `live.py` | Healthy controls, injection, symptoms and persistence checks |
| `agent_view.py` | Agent-visible observations and injection-artifact scans |
| `environment.py`, `admission.py` | Stage records and admission decisions |
| `provenance.py` | Source and configuration evidence |
| `coverage.py` | Match audits to release cases and report gaps |
| `report_doc.py` | Render the historical release audit reference from retained records |

This README documents the retained runner. A successful CLI check does not establish that its old task inventory or runtime APIs match a new benchmark release.
