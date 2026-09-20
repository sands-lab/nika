# Browse sessions with `nika inspect`

How-to for operators who want a browser UI over session and benchmark results, and for maintainers who change the React front end.

The CLI flag surface lives in the [CLI reference: `nika inspect`](cli-reference.md#nika-inspect).

## Extra setup (source checkout)

From a git checkout, build the front end once. Wheel installs already include `www/dist`.

1. Install [Node.js](https://nodejs.org/) 18+ (includes `npm`). Confirm with `node -v` and `npm -v`.
2. Build the UI:

```shell
cd src/nika/view/www
npm install
npm run build
```

## Run

```shell
uv run nika inspect --result-dir results
```

Default URL: `http://127.0.0.1:7580/`. Override the results root with `--result-dir` or `nika.result_dir` in [`config/nika.yaml`](configuration.md).

To reach the UI from another machine (or Windows from WSL), bind all interfaces and open the host IP:

```shell
uv run nika inspect --result-dir results --host 0.0.0.0
# then open http://<this-host-ip>:7580/
```

`--host` accepts `127.0.0.1`, `localhost`, `::1`, `0.0.0.0`, or `::`. Specific interface IPs are rejected.

Success: stderr prints `url: http://127.0.0.1:<port>/` and the browser shows the session list for that results root.

## Change the UI

Source: [`src/nika/view/www`](../../src/nika/view/www).

Hot reload:

```shell
cd src/nika/view/www
npm install
npm run dev
```

Proxy `/api` to a running `nika inspect --no-open` on port 7580.

Production build (writes `dist/`):

```shell
cd src/nika/view/www
npm run build
```
