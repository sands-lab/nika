# Browse sessions with `nika inspect`

How-to for operators who want a browser UI over session and benchmark results, and for maintainers who change the React front end.

The CLI flag surface lives in the [CLI reference: `nika inspect`](cli-reference.md#nika-inspect).

## Extra setup (source checkout)

From a git checkout, build the front end once. Wheel installs already include `www/dist`.

1. Install [Node.js](https://nodejs.org/) 18+ (includes `npm`). Confirm with `node -v` and `npm -v`.
2. Build the UI:

```shell
cd src/nika/inspect/www
npm install
npm run build
```

## Run

```shell
uv run nika inspect --result-dir results
```

Default URL: `http://127.0.0.1:7580/`. Override the results root with `--result-dir` or `nika.result_dir` in [`config/nika.yaml`](configuration.md).

`nika benchmark run` on a TTY starts this viewer automatically for the run's `--result_dir` and shows a clickable URL on the dashboard. You can still run `nika inspect` yourself for finished results.

To reach the UI from another machine (or Windows from WSL), bind all interfaces and open the host IP:

```shell
uv run nika inspect --result-dir results --host 0.0.0.0
# then open http://<this-host-ip>:7580/
```

`--host` accepts `127.0.0.1`, `localhost`, `::1`, `0.0.0.0`, or `::`. Specific interface IPs are rejected.

With `--host 0.0.0.0` or `--host ::`, the UI is read-only: **Delete** and annotation edits return `403`, and the folder picker cannot leave `--result-dir`. With a loopback `--host`, the server answers only requests addressed to `127.0.0.1`, `localhost`, or `::1`. To delete sessions from another machine, keep the loopback bind and forward the port:

```shell
ssh -L 7580:127.0.0.1:7580 <host>
```

Success: stderr prints `url: http://127.0.0.1:<port>/` and the browser shows the session list for that results root.

The Raw tab lists the `.json`, `.jsonl`, `.txt`, `.log`, and `.md` files at the top of the session directory, with the standard artifacts first. It does not open subdirectories or symlinks that point outside the session. Files over 16 MB show only their first 16 MB.

## Review sessions

### Share a view

The address bar tracks the results root, open session, tab, source filter, selected event, and in-session search, for example `/?session=<id>&source=agent&event=agent-61&find=traceroute`. Copy the URL to share the exact event, or reload to restore it. Browser back and forward move between the list and sessions.

### Navigate a trajectory

The Timeline tab shows the agent and NIKA logs merged in time order. **All**, **Agent**, and **NIKA** in the session header narrow it to one source.

Long values in the inspector end with a `… [N chars total]` marker. **Show more** loads the full event from the log.

On the Timeline tab:

| Key | Action |
|-----|--------|
| `j` / `k` | Select the next or previous event |
| `/` | Focus **Find in trajectory** |
| `Enter` / `Shift+Enter` | Jump to the next or previous match |
| `Esc` | Leave the find box so `j` / `k` work again |

Find matches the full agent and NIKA log records, including tool inputs and outputs, case-insensitively. The viewer highlights matching rows and shows the match count.

### Read the time breakdown

The Overview tab opens with a **Time breakdown** block built from the same merged log as the Timeline tab. Two stacked bars show where the wall clock went:

- **Session wall clock**: lab setup (`env_start`), fault inject, the agent run (`agent_start` to `agent_end`), lab teardown (`env_stop`), and the gaps between them.
- **Inside the agent run**: LLM requests, tool execution, sandbox setup, and the remaining agent overhead (CLI startup, MCP attach, parsing, idle).

Tool time takes priority over LLM time where the two overlap, so a Codex turn that runs tools reports the tool part as tool execution. The headline chips add the LLM share, the request count, average / median / max request latency, and output tokens per second of LLM time. Running sessions mark both bars `live` and refresh whenever the timeline poll brings new log rows.

Below the bars, **Tools** lists calls, errors, and total / average / max time per tool, and **Slowest operations** ranks the longest LLM requests, tool calls, and NIKA steps. Click an operation to open it on the Timeline tab.

### Search across sessions

**Search sessions** filters on the session id, scenario, agent, model, fault, case, benchmark fields, and tags. It does not read `ground_truth.json` or score files.

### Choose columns

Right-click the table header, or click **⋯** at the end of the header row, to choose columns, including detection, localization F1, steps, tool calls, token counts, backend, and tags. Score and count columns accept numeric filters such as `>=0.5`, `<10`, or `=0`. A bare number means `=`.

The browser keeps the column choice in `localStorage`, so it stays local to that browser.

### Tag sessions

Add free-form tags in the session header and filter the list by them in the Tags column.

The server writes tags to `annotations.json` in the session directory. Edits require a loopback `--host` and a session that has stopped running; otherwise the server refuses the write.

## Change the UI

Source: [`src/nika/inspect/www`](../../src/nika/inspect/www).

Hot reload:

```shell
cd src/nika/inspect/www
npm install
npm run dev
```

Proxy `/api` to a running `nika inspect --no-open` on port 7580.

Production build (writes `dist/`):

```shell
cd src/nika/inspect/www
npm run build
```
