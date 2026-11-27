# The hosted copy (fly.toml, README "Running in the cloud"). The local
# double-click launcher does not use this file.
#
# 3.13 because that is what the dev venvs run, so the image matches what the
# suite is run against day to day.
FROM python:3.13-slim

# tzdata so fly.toml's TZ means something. date.today() (api._today) reads the
# process's local zone, and without the zone files TZ=Europe/Helsinki silently
# falls back to UTC, putting "today" a day behind for the first hours after
# local midnight.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.13 /uv /bin/uv

WORKDIR /app

# Dependencies first, from the lockfile alone, so a code-only push reuses this
# layer. --locked, not --frozen: refuse to build if uv.lock has drifted from
# pyproject.toml, because the lockfile is what CI tested and what keeps PuLP
# below 4. The solver extra is in: Optimize is part of the hosted app, and
# PuLP bundles the linux CBC binary.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-install-project --extra solver

# Then the app itself, installed editable like the dev setup. A regular wheel
# would leave out web/static: nothing declares those files as package data.
COPY buschain ./buschain
RUN uv sync --locked --extra solver

ENV PATH="/app/.venv/bin:$PATH"

# /data is deliberately NOT created here. It only exists once fly.toml's volume
# is mounted on it, so a machine started without the volume fails to open the
# database at boot instead of quietly keeping the plan on a disk that the next
# deploy throws away.
#
# One process, one worker, always: the store serializes writes with an
# in-process lock and keeps undo snapshots in the same SQLite file, so a second
# worker would be a second writer that lock knows nothing about.
EXPOSE 8080
CMD ["uvicorn", "--factory", "buschain.web.cloud:build_app", "--host", "0.0.0.0", "--port", "8080"]
