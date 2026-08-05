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
COPY controller ./controller
RUN pip install .

EXPOSE 8080
USER 1001

# One image, two entrypoints: the build controller Deployment overrides this
# with `python -m controller.main` (docs/BUILDING.md - Ownership). Two services
# built, scanned and released together cannot drift in the library they share.
ENTRYPOINT ["python", "-m", "api.main"]
