FROM python:3.12-slim AS app-base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN groupadd --system app \
    && useradd --system --gid app --home-dir /app --no-create-home app

COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir -r /app/requirements.txt

COPY --chown=app:app . /app
RUN mkdir -p /app/runtime /app/study_data \
    && chown -R app:app /app/runtime /app/study_data

FROM app-base AS smoke

USER app
CMD ["python", "-m", "uvicorn", "tests.docker_smoke_app:app", "--host", "0.0.0.0", "--port", "8080"]

FROM app-base AS runtime

ENV REPORT_AGENT_CONTAINER=1 \
    REPORT_AGENT_RUNTIME_ROOT=/app/runtime \
    REPORT_AGENT_STUDY_ROOT=/app/study_data \
    REPORT_AGENT_STATIC_DIR=/app/frontend/dist \
    REPORT_AGENT_CHECKPOINT_DB_PATH=/app/runtime/agent_memory_fastapi.db

USER app
EXPOSE 8080
CMD ["python", "run_fastapi.py", "--host", "0.0.0.0", "--port", "8080"]

