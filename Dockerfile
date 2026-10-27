# syntax=docker/dockerfile:1.7
FROM python:3.12-slim AS runtime

ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_TRUSTED_HOST=pypi.org files.pythonhosted.org

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=300 \
    PIP_RETRIES=10 \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_TRUSTED_HOST=${PIP_TRUSTED_HOST} \
    PYTHONPATH=/app/src \
    COMPLIANCE_ENV=production

WORKDIR /app

# FAISS and numpy need libgomp; curl is used by the container healthcheck.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml requirements.txt ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --index-url ${PIP_INDEX_URL} --trusted-host ${PIP_TRUSTED_HOST} --prefer-binary .

COPY corpus ./corpus
COPY prompts ./prompts
COPY web ./web
COPY services ./services
COPY scripts ./scripts
COPY evals ./evals
COPY loadtest ./loadtest

RUN useradd --create-home --uid 10001 compliance \
    && mkdir -p /data/index /data/quarantine \
    && chown -R compliance:compliance /app /data

USER compliance

EXPOSE 8000 9101 8080
HEALTHCHECK --interval=20s --timeout=4s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health/live || exit 1

CMD ["python", "-m", "compliance.cli", "serve", "--host", "0.0.0.0", "--port", "8000"]

# Load-test image.  Keeping locust out of the production image while still allowing one-command load
# tests against the compose stack.
FROM runtime AS loadtest

USER root
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --index-url ${PIP_INDEX_URL} --trusted-host ${PIP_TRUSTED_HOST} --prefer-binary "locust>=2.31,<3.0"
USER compliance

