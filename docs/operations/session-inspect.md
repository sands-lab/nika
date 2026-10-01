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

While a session has status `running`, the viewer hides its answer key and scores. Requests for `ground_truth.json`, `eval_metrics.json`, or `llm_judge.json` return `403`, and the scores view shows only the submission. The files appear once the session stops.

## Review sessions

### Share a view

The address bar tracks the results root, open session, tab, selected event, and in-session search, for example `/?session=<id>&tab=timeline&event=agent-61&find=traceroute`. Copy the URL to share the exact event, or reload to restore it. Browser back and forward move between the list and sessions.

### Navigate a trajectory

On the Timeline, Agent, and NIKA tabs:

| Key | Action |
|-----|--------|
| `j` / `k` | Select the next or previous event |
| `/` | Focus **Find in trajectory** |
| `Enter` / `Shift+Enter` | Jump to the next or previous match |
| `Esc` | Leave the find box so `j` / `k` work again |

Find matches the full agent and NIKA log records, including tool inputs and outputs, case-insensitively. The viewer highlights matching rows and shows the match count.

### Search across sessions

**Search sessions** filters on summary fields and tags. **Search trajectories** scans each session's `messages.jsonl` and `nika.jsonl`, keeps sessions with hits, and shows the hit count in the Failure cell. Opening a session from those results carries the query into Find. Neither search reads `ground_truth.json` or score files.

### Choose columns and save views

**Columns** toggles table columns, including detection, localization F1, steps, tool calls, token counts, backend, and tags. Score and count columns accept numeric filters such as `>=0.5`, `<10`, or `=0`. A bare number means `=`.

**Save view** stores the current filters, columns, and sort under a name. **Views** restores a saved view. The browser keeps columns and views in `localStorage`, so they stay local to that browser.

### Tag sessions and comment on events

Add free-form tags in the session header and filter the list by them in the Tags column. Select an event to add or delete review comments in the inspector; the timeline marks commented events with `✎`.

The server writes tags and comments to `annotations.json` in the session directory. Edits require a loopback `--host` and a session that has stopped running; otherwise the server refuses the write.

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
