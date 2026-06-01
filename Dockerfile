FROM python:3.14-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Pure stdlib — no dependencies to install.
COPY hestia/ ./hestia/
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
