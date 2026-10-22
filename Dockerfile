# Base images and downloads are pinned by digest or checksum so a rebuild of
# the same commit installs the same bits. Bump them deliberately.
FROM ghcr.io/astral-sh/uv:0.10.8@sha256:88234bc9e09c2b2f6d176a3daf411419eb0370d450a08129257410de9cfafd2a AS uv

FROM python:3.11-slim@sha256:cce12316475d7d56d605f76e1d811d0dc4e7227f2d4afd29951521264f504ff8

LABEL org.opencontainers.image.source="https://github.com/python-roborock/local_roborock_server" \
  org.opencontainers.image.description="Private Roborock HTTPS and MQTT stack for local LAN use" \
  org.opencontainers.image.licenses="MIT"

ARG ACME_SH_VERSION=3.1.6
ARG ACME_SH_SHA256=0d3f9000ac44a6331314742a88c475f79134e24fc991997883652adc59efc486

RUN apt-get update \
  && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    coturn \
    mosquitto \
    openssl \
  && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL -o /tmp/acme.sh.tar.gz "https://github.com/acmesh-official/acme.sh/archive/refs/tags/${ACME_SH_VERSION}.tar.gz" \
  && echo "${ACME_SH_SHA256}  /tmp/acme.sh.tar.gz" | sha256sum -c - \
  && mkdir -p /opt/acme.sh \
  && tar -xzf /tmp/acme.sh.tar.gz --strip-components=1 -C /opt/acme.sh \
  && rm /tmp/acme.sh.tar.gz \
  && chmod +x /opt/acme.sh/acme.sh \
  && ln -sf /opt/acme.sh/acme.sh /usr/local/bin/acme.sh

WORKDIR /app

COPY pyproject.toml uv.lock README.md /app/
COPY src /app/src

# Install the exact dependency versions and hashes from uv.lock, then the
# project itself without letting pip resolve anything else.
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
  uv export --frozen --no-dev --no-emit-project --format requirements-txt -o /tmp/requirements.txt \
  && pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt \
  && pip install --no-cache-dir --no-deps /app \
  && rm /tmp/requirements.txt

EXPOSE 555 8881 3478/udp

CMD ["python", "-m", "roborock_local_server.container_entrypoint"]
