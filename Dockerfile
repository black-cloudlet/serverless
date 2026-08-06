FROM python:3.14-slim

ARG PIP_INDEX_URL
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml ./
COPY api ./api
COPY common ./common
RUN pip install ".[api]"

EXPOSE 8080
USER 1001

ENTRYPOINT ["python", "-m", "api.main"]
