FROM node:22-bookworm-slim AS ui-build

WORKDIR /app/ui
COPY ui/package*.json ./
RUN npm ci
COPY ui/ ./
RUN npm run build


FROM python:3.14-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# System binary for the OPTIONAL local 433 MHz outdoor-temp feeder
# (HESTIA_OUTDOOR_TEMP_SOURCE=local). hestia.sensor433 SPAWNS `rtl_433` (it is an
# external binary, not a Python import); it is pointed at a host-side rtl_tcp via
# HESTIA_RTL433_DEVICE=rtl_tcp:HOST:PORT (host networking → 127.0.0.1:1234), so NO
# USB device is passed into the container. Inert when the feeder is off — the
# default source (open-meteo) never invokes it. Installed in its own early layer
# so it isn't busted by app-code changes.
RUN apt-get update \
    && apt-get install -y --no-install-recommends rtl-433 \
    && rm -rf /var/lib/apt/lists/*

# Runtime deps (cryptography — the Tuya AES primitive). Copied + installed before the
# source so an app-code change doesn't bust the cached dependency layer.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY hestia/ ./hestia/
COPY --from=ui-build /app/ui/dist ./ui/dist/
# Ship the LG A/C IR signal DB so the dashboard AC panel appears. The default
# HESTIA_KLIMA_IR resolves to dirname(dirname(__file__))/tools/klima.ir =
# /app/tools/klima.ir, so no env override is needed. (klima.ir is a generated,
# install-independent protocol asset — baked in, not state; runtime state lives
# on the /data volume, see docker-compose.yml.)
COPY tools/klima.ir ./tools/klima.ir

# A volume mounts over /data at runtime; create+own it so a no-mount / named-volume
# run is still writable by the non-root uid.
RUN mkdir -p /data && chown 1000:1000 /data

# Cosmetic under network_mode: host (EXPOSE is a no-op there) — documents the
# ports: 8925 device-facing, 8926 control (loopback), 8927 web UI (loopback).
EXPOSE 8925 8926 8927

# Run NON-ROOT (uid:gid 1000:1000). On the appliance host this matches the operator
# account, so the bind-mounted /data state stays host-readable/editable; serial
# device (dialout) + lease-file (pihole) access is granted via group_add in compose.
# hestia uses no passwd/HOME lookups, so a bare numeric uid is fine.
USER 1000:1000

# Resolves HESTIA_MODE from env or the persisted registry.mode (Phase-3
# graduation), then runs proxy (default) or standalone.
CMD ["python", "-m", "hestia"]
