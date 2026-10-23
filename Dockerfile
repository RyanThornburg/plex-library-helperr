# syntax=docker/dockerfile:1

# both stages must use the same image, the venv links to its python
ARG PYTHON_IMAGE=python:3.14-slim

FROM ghcr.io/astral-sh/uv:0.13.4 AS uv

FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

FROM ${PYTHON_IMAGE}
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    COLUMNS=160 \
    DATA_DIR=/config
WORKDIR /app
# app files stay root-owned so the app can't modify its own code
COPY --from=build /app/.venv /app/.venv
COPY *.py ./
COPY config/__init__.py config/models.py config/config.toml.example ./config/
# unraid's nobody:users, so files written to /config match other appdata.
# a new named volume copies this ownership; a bind mount needs chown on the host
RUN mkdir /config && chown 99:100 /config
USER 99:100
VOLUME /config
ENTRYPOINT ["python", "main.py"]
