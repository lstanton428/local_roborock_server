# Base images and downloads are pinned by digest or checksum so a rebuild of
# the same commit installs the same bits. Bump them deliberately.
FROM ghcr.io/astral-sh/uv:0.10.8@sha256:88234bc9e09c2b2f6d176a3daf411419eb0370d450a08129257410de9cfafd2a AS uv

# coturn is built from a pinned upstream release because Debian 13 ships 4.6.1,
# which has unfixed unauthenticated STUN/TURN bugs (for example CVE-2026-104074
# and CVE-2026-68552). Only turnserver is built, without database backends: the
# add-on uses a static lt-cred-mech user. Bump COTURN_VERSION and COTURN_SHA256
# together by hand; get the checksum with
#   curl -fsSL https://github.com/coturn/coturn/archive/refs/tags/<version>.tar.gz | sha256sum
FROM python:3.11-slim@sha256:07d4824036af339fa1b6ef769cc99d9c10a141769538b52b3b597c84f7a7b535 AS coturn

ARG COTURN_VERSION=4.19.2
ARG COTURN_SHA256=2c80e4dffd0eb410951ba469f5cf8bd9b4a0658afc72d816f5c519c0c988d75c

RUN apt-get update \
  && apt-get install -y --no-install-recommends \
    build-essential \
    ca-certificates \
    cmake \
    curl \
    libevent-dev \
    libssl-dev \
    pkg-config \
  && curl -fsSL -o /tmp/coturn.tar.gz "https://github.com/coturn/coturn/archive/refs/tags/${COTURN_VERSION}.tar.gz" \
  && echo "${COTURN_SHA256}  /tmp/coturn.tar.gz" | sha256sum -c - \
  && mkdir -p /tmp/coturn \
  && tar -xzf /tmp/coturn.tar.gz --strip-components=1 -C /tmp/coturn \
  && cmake -S /tmp/coturn -B /tmp/coturn/build -DCMAKE_BUILD_TYPE=Release \
  && cmake --build /tmp/coturn/build --target turnserver -j"$(nproc)" \
  && install -D -m 0755 /tmp/coturn/build/bin/turnserver /opt/coturn/turnserver \
  && /opt/coturn/turnserver --version

FROM python:3.11-slim@sha256:07d4824036af339fa1b6ef769cc99d9c10a141769538b52b3b597c84f7a7b535

LABEL org.opencontainers.image.source="https://github.com/python-roborock/local_roborock_server" \
  org.opencontainers.image.description="Private Roborock HTTPS and MQTT stack for local LAN use" \
  org.opencontainers.image.licenses="MIT"

# Dependabot does not track this download: bump ACME_SH_VERSION and
# ACME_SH_SHA256 together by hand. Get the new checksum with
#   curl -fsSL https://github.com/acmesh-official/acme.sh/archive/refs/tags/<version>.tar.gz | sha256sum
ARG ACME_SH_VERSION=3.1.6
ARG ACME_SH_SHA256=0d3f9000ac44a6331314742a88c475f79134e24fc991997883652adc59efc486

RUN apt-get update \
  && apt-get upgrade -y \
  && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    libevent-2.1-7t64 \
    libevent-core-2.1-7t64 \
    libevent-extra-2.1-7t64 \
    libevent-openssl-2.1-7t64 \
    libevent-pthreads-2.1-7t64 \
    mosquitto \
    openssl \
  && rm -rf /var/lib/apt/lists/*

COPY --from=coturn /opt/coturn/turnserver /usr/local/bin/turnserver
RUN if ldd /usr/local/bin/turnserver | grep -q "not found"; then ldd /usr/local/bin/turnserver; exit 1; fi \
  && turnserver --version

RUN curl -fsSL -o /tmp/acme.sh.tar.gz "https://github.com/acmesh-official/acme.sh/archive/refs/tags/${ACME_SH_VERSION}.tar.gz" \
  && echo "${ACME_SH_SHA256}  /tmp/acme.sh.tar.gz" | sha256sum -c - \
  && mkdir -p /opt/acme.sh \
  && tar -xzf /tmp/acme.sh.tar.gz --strip-components=1 -C /opt/acme.sh \
  && rm /tmp/acme.sh.tar.gz \
  && chmod +x /opt/acme.sh/acme.sh \
  && ln -sf /opt/acme.sh/acme.sh /usr/local/bin/acme.sh

WORKDIR /app

COPY pyproject.toml uv.lock build-requirements.txt README.md /app/
COPY src /app/src

# Install the exact dependency versions and hashes from uv.lock; --locked fails
# the build if uv.lock is out of date with pyproject.toml. The build backend
# (hatchling) comes from the hash-pinned build-requirements.txt into a
# throwaway directory and is used via --no-build-isolation, so pip never
# fetches an unpinned backend. It is removed afterwards and not shipped.
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
  uv export --locked --no-dev --no-emit-project --format requirements-txt -o /tmp/requirements.txt \
  && pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt \
  && pip install --no-cache-dir --require-hashes --target /tmp/build-deps -r /app/build-requirements.txt \
  && PYTHONPATH=/tmp/build-deps pip install --no-cache-dir --no-deps --no-build-isolation /app \
  && rm -rf /tmp/requirements.txt /tmp/build-deps

EXPOSE 555 8881 3478/udp

CMD ["python", "-m", "roborock_local_server.container_entrypoint"]
