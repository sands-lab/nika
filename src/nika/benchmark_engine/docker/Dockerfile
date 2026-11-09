# Benchmark engine image of one NIKA release (contract B).
#
# Build context: `git archive` of the release tag, so the image pins that
# release's code, Python dependencies, and emulator tooling. `nika engine build`
# builds it. Environments run as sibling containers through the host's Docker
# socket.
FROM python:3.12-slim-bookworm

ARG NIKA_COMMIT=""
# Commit time of NIKA_COMMIT. Without a uv.lock in the tree, dependencies
# resolve as of this time, so rebuilding a tag gives the same versions.
ARG NIKA_COMMIT_DATE=""
ARG CLAB_VERSION=0.79.0
ARG GNMIC_VERSION=0.48.0

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl iproute2 iptables kmod passwd procps sudo util-linux \
 && rm -rf /var/lib/apt/lists/*

COPY --from=docker:28-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=ghcr.io/astral-sh/uv:0.12.10 /uv /usr/local/bin/uv

# Containerlab needs root; like the host install, it is setuid root.
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "${arch}" in amd64) gnmic_arch=x86_64 ;; arm64) gnmic_arch=aarch64 ;; esac; \
    curl -fsSL --retry 4 --retry-max-time 120 \
      "https://github.com/srl-labs/containerlab/releases/download/v${CLAB_VERSION}/containerlab_${CLAB_VERSION}_linux_${arch}.tar.gz" \
      -o /tmp/containerlab.tar.gz; \
    tar -xzf /tmp/containerlab.tar.gz --no-same-owner -C /usr/local/bin containerlab; \
    rm /tmp/containerlab.tar.gz; \
    chown root:root /usr/local/bin/containerlab; \
    chmod 4755 /usr/local/bin/containerlab; \
    ln -s containerlab /usr/local/bin/clab; \
    curl -fsSL --retry 4 --retry-max-time 120 \
      "https://github.com/openconfig/gnmic/releases/download/v${GNMIC_VERSION}/gnmic_${GNMIC_VERSION}_Linux_${gnmic_arch}.tar.gz" \
      -o /tmp/gnmic.tar.gz; \
    tar -xzf /tmp/gnmic.tar.gz --no-same-owner -C /usr/local/bin gnmic; \
    rm /tmp/gnmic.tar.gz

WORKDIR /opt/nika
ENV UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    PATH=/opt/nika/.venv/bin:$PATH
COPY . .
RUN if [ -f uv.lock ]; then uv sync --frozen --no-dev; \
    else uv sync --no-dev --exclude-newer "${NIKA_COMMIT_DATE}"; fi \
 && chmod -R a+rX /opt/nika

# The source has no .git; describe reports this commit instead.
ENV NIKA_ENGINE_COMMIT=${NIKA_COMMIT}

ENTRYPOINT ["/opt/nika/src/nika/benchmark_engine/docker/entrypoint.sh"]
