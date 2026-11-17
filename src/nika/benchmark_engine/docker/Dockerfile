# Benchmark engine image of NIKA release 0.1.0 (contract B).
#
# Build context: `git archive` of a commit on `release/0.1.0`, so the image
# pins that code and its Python dependencies (`uv.lock`, resolved as of the
# release's base commit, 249d5fdb of 2026-08-15). The frontend builds it with
# `nika engine build`. Environments run as sibling containers through the
# host's Docker socket. Release 0.1.0 deploys only Kathara labs, so the image
# carries no Containerlab or gNMI tooling.
FROM python:3.12-slim-bookworm

ARG NIKA_COMMIT=""
# Passed by the frontend's build; dependencies come from uv.lock instead.
ARG NIKA_COMMIT_DATE=""

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates passwd procps util-linux \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.12.10 /uv /usr/local/bin/uv

WORKDIR /opt/nika
ENV UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    PATH=/opt/nika/.venv/bin:$PATH
COPY . .
RUN uv sync --frozen --no-dev --extra labs \
 && chmod -R a+rX /opt/nika

# The source has no .git; describe reports this commit instead.
ENV NIKA_ENGINE_COMMIT=${NIKA_COMMIT}

ENTRYPOINT ["/opt/nika/src/nika/benchmark_engine/docker/entrypoint.sh"]
