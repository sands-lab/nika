# Interactive shell (`nika tui`)

`nika tui` opens an interactive shell for people who would rather pick than type flags. It sits on top of the regular CLI: every action runs a normal `nika …` command in a subprocess and prints that command first, so you can copy it into a script later. The CLI keeps working exactly as before.

```shell
uv run nika                                       # bare `nika` in a terminal opens the shell
uv run nika tui --run-config config/cli.claude.yaml
```

Bare `nika` opens the shell only in an interactive terminal. In scripts and pipes it still fails with "Missing command", and `nika --help` still prints the command list. Use `nika tui --run-config PATH` to start with a specific agent profile.

The home screen shows the active agent profile, whether its LLM endpoint is up, the number of running sessions, and a network "diagram of the day". The bottom bar keeps the profile, endpoint status, session count, and task count visible while you work.

## Commands

Type `/` to open the command menu. Tab completes commands, subcommands, options, and task labels.

| Command | What it does | CLI equivalent |
| --- | --- | --- |
| `/run [LABEL]` | Pick a task, an injection preset, and an agent profile; check the endpoint; review the options; run | `nika agent run --problem LABEL` |
| `/tasks [FILTER]` | Search tasks by label, failure domain, or symptom; run one, start its lab without an agent, or describe the failure | `nika case browse`, `nika case run` |
| `/agents` | List run configs under `config/` (your copies and the `*.example.yaml` templates) with live endpoint status; pick one to make it active | `nika agent check` |
| `/use [PATH]` | Switch the active profile | `--run-config PATH` |
| `/check` | Check the active endpoint: reachable, credentials accepted, model served | `nika agent check` |
| `/sessions` | Pick a running session, then open a host shell, run an agent, inject a failure, inspect, evaluate, or close it | `nika session …` |
| `/lab [SCENARIO]` | Deploy a scenario without a fault | `nika env run` |
| `/inject` | Inject a failure into a running session | `nika failure inject` |
| `/shell` | Interactive shell on a lab host; each line runs through `nika exec` | `nika exec` |
| `/bench` | Run a frozen release split with an agent, or list releases and their tasks | `nika benchmark run --release …` |

Every CLI group is also a slash command. With arguments it runs directly (`/session ps -a`, `/failure describe link_down`). Without arguments it lets you choose a subcommand and opens a form for its options. Form fields that name a session, host, scenario, failure, task, agent profile, release, or split open a searchable picker. A line that starts with `nika ` runs as typed.

`/help` lists all commands, and `/help CMD` prints the CLI help for that command. Press Esc to go back one step and Ctrl+C to cancel. At the prompt, Ctrl+C clears the line; press it twice within two seconds, or use Ctrl+D or `/quit`, to leave.

## Endpoint checks

The endpoint check lists models at the provider's API (`GET …/models`) with the credentials the agent will use. It does not send a completion, so it costs no tokens. Results:

| Status | Meaning |
| --- | --- |
| `up` | The endpoint answered and lists the configured model |
| `up, model not listed` | The endpoint answered but does not list the model; check `agent.model` |
| `no credentials` | The provider API key is missing from `.env`, or `agent.base_url` is unset for `provider: custom` |
| `auth rejected` | The endpoint returned HTTP 401 or 403 |
| `unreachable` | Connection failed or timed out |

`/run` and `/bench` check the endpoint before they start and ask before running against an endpoint that did not pass. Use `nika agent check [--run-config PATH] [--json]` for the same check in scripts. It exits non-zero when the endpoint is not usable.

## Task catalog

`/run` and `/tasks` list task labels from the same catalog as `nika case browse`: the working pool plus generated cases for registered failures. Generating cases for some scenarios needs Docker. Without it, the shell falls back to the working pool and says so. Only scenario-default deployments are listed, because `nika agent run --problem` cannot pass ISP deployment options; use `nika case browse` for those.
