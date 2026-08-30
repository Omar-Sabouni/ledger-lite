# syntax=docker/dockerfile:1.7

FROM node:22-alpine AS frontend-build

WORKDIR /workspace/frontend

COPY frontend/package.json frontend/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm \
    npm ci

COPY frontend/ ./
RUN npm run build


FROM python:3.12-slim AS python-build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /workspace

COPY pyproject.toml README.md requirements.txt requirements-dev.txt ./
COPY app/ ./app/

RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --constraint requirements-dev.txt hatchling \
    && python -m pip wheel --no-build-isolation --constraint requirements.txt \
       --wheel-dir /wheels .


FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TMPDIR=/tmp \
    PATH="/usr/local/bin:${PATH}"

WORKDIR /app

RUN addgroup --system --gid 10001 ledgerlite \
    && adduser --system --uid 10001 --ingroup ledgerlite --home /nonexistent ledgerlite

COPY --from=python-build /wheels /wheels
RUN python -m pip install --no-cache-dir --no-index --find-links=/wheels ledger-lite \
    && rm -rf /wheels

COPY alembic.ini ./
COPY alembic/ ./alembic/
COPY --from=frontend-build /workspace/frontend/dist ./frontend/dist/

USER 10001:10001

EXPOSE 8000
STOPSIGNAL SIGTERM

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--no-server-header"]
