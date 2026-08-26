FROM python:3.12.11-slim@sha256:47ae396f09c1303b8653019811a8498470603d7ffefc29cb07c88f1f8cb3d19f AS builder

ENV UV_LINK_MODE=copy UV_NO_PROGRESS=1
WORKDIR /app
RUN pip install --no-cache-dir uv==0.8.17
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.12.11-slim@sha256:47ae396f09c1303b8653019811a8498470603d7ffefc29cb07c88f1f8cb3d19f
RUN groupadd --system playtrace && useradd --system --gid playtrace --no-create-home playtrace
WORKDIR /app
COPY --from=builder --chown=playtrace:playtrace /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN /app/.venv/bin/python -c "import playtrace, playtrace.sim_worker"
USER playtrace:playtrace
CMD ["python", "-m", "playtrace.sim_worker"]
