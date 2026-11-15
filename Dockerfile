# reprobouncer — containerized audit + headless Orbit reproduction.
#
# Static audits need only the python layer; --full additionally execs
# Orbit (galaxyproject/orbit @ d06b334d, pi-coding-agent) under node
# >=22.19. Orbit runs shell commands on the host it's invoked from —
# inside this container that host IS the container, which is the
# containment boundary: the reproduction agent can only touch the
# container filesystem, not yours.
#
# Build:  docker build -t reprobouncer .
# Run:    docker run --rm --env-file .env -v "$PWD/out:/out" \
#           reprobouncer audit 'doi:…' -o /out/report.json
# Full:   … audit 'doi:…' --full -k 1 -o /out/report.json
#
# Orbit's LLM provider is env-keyed (ORBIT_ACTIVE_LLM_API_KEY, forwarded
# from LLM_API_KEY via REPRO_ORBIT_HOME) — the eval's isolated-home
# pattern, so no host ~/.orbit mount and no encrypted keys needed.
# Provider endpoint/model default to TACC Tejas / MiniMax-M3; override
# with ORBIT_LLM_BASE_URL / ORBIT_LLM_MODEL.

FROM node:22-bookworm-slim AS orbit
ARG ORBIT_COMMIT=d06b334d763db1db1907186b5b079844437545e5
RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN git clone https://github.com/galaxyproject/orbit /opt/orbit \
    && cd /opt/orbit && git checkout $ORBIT_COMMIT && npm ci

FROM node:22-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=orbit /opt/orbit /opt/orbit
ENV ORBIT_BIN=/opt/orbit/bin/orbit.js \
    REPRO_ORBIT_HOME=/orbit-home \
    REPRO_RUNS_DIR=/runs

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip3 install --no-cache-dir --break-system-packages .

COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh \
    && mkdir -p /runs /out

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["--help"]
