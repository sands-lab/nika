# NIKA research experiments

Use the `experiment` branch to reproduce and extend the NIKA paper's experiments. Keep research runners, ablations and configurations on this branch. Develop reusable runtime fixes on a development branch and bring them here when needed.

Run commands from the repository root after following the [installation guide](../docs/operations/installation.md). Each experiment's README defines its additional tools, inputs and outputs.

| Experiment | Question | Entry point and scope |
|---|---|---|
| [Scenario validation](scenario_validation/README.md) | Do generated configurations satisfy their declared requirements? | Synthesis, static and live checks; coverage depends on the declared deployment capabilities |
| [Environment audit](audit/README.md) | Does a task expose the intended healthy and faulty behavior? | Docker-backed admission checks for the historical release inventory |
| [Start-state consistency](convergence/README.md) | Do redeploys or restores reach equivalent forwarding states? | Native/Firecracker collection and KATch analysis |
| [Resource profiling](profile/README.md) | What time and resources does a lab consume? | Lifecycle timing and cgroup sampling |
| [Snapshot reset timing](snapshot_reset/README.md) | What does snapshot restore save over deployment or recovery? | Historical collectors and combined analysis; adapt to the frozen runtime before new measurements |
| [Guest clock and DHCP](clock_restore/README.md) | Does advancing a restored clock change lease behavior? | Historical clock-step control; the no-clock-step comparison remains to be implemented |
| [Submission audit](submission_audit/README.md) | How well does the audit pipeline recover labelled defects? | Retained labels, reference mapping and component evaluation |
| [Audit evolution](audit_evolution/README.md) | Does accumulated audit knowledge improve later audits? | Sequential replay, baseline arms, planted defects and scoring |

NetOpsBench and NetArena lifecycle collectors live in the separate [network-benchmark-experiments](https://github.com/zhihao1998/network-benchmark-experiments) repository. NIKA-specific collection and combined paper analysis stay here. The NetOpsBench trajectory adapter in `audit_evolution/` belongs to the audit experiment.

Store run artifacts outside the source tree or in ignored runtime directories on local disk. Freeze the source commit, deployment inventory, images, tool versions and measurement boundaries before collecting new paper results. Existing historical results do not validate a new release or runtime. Close only sessions and other resources created by your run.
