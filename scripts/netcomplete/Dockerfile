# syntax=docker/dockerfile:1
FROM python:2.7-slim@sha256:6c1ffdff499e29ea663e6e67c9b6b9a3b401d554d2c9f061f9a45344e3992363

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN sed -i 's/deb.debian.org/archive.debian.org/g' /etc/apt/sources.list && \
    sed -i 's|security.debian.org|archive.debian.org|g' /etc/apt/sources.list

# System dependencies required to build z3 and python packages
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        git \
        libgmp-dev \
        python-dev \
    && rm -rf /var/lib/apt/lists/*

# Build and install z3 from the pinned commit
RUN git clone https://github.com/Z3Prover/z3.git /tmp/z3 \
    && cd /tmp/z3 \
    && git checkout a63d1b184800954aef888fb76d531237f574f957 \
    && python scripts/mk_make.py --python \
    && cd build \
    && make -j4 \
    && make install \
    && ldconfig \
    && rm -rf /tmp/z3

# Install Tekton from GitHub
RUN pip install git+https://github.com/nsg-ethz/tekton.git@b60e931334e690919d12a5826600e5c400b6173d#egg=Tekton

WORKDIR /workspace/synet-plus

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

ENV PYTHONPATH=/workspace/synet-plus

COPY . ./

# Patch Tekton to fix ipaddress unicode issue in Python 2.7
RUN sed -i "s/ip_network(str(network))/ip_network(unicode(network))/g" \
    /usr/local/lib/python2.7/site-packages/tekton/cisco.py

CMD ["python2"]
