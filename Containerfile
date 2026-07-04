# Airgap-friendly image. Base + pip index are mirrored internally (docs §9);
# build args let the pipeline point at the internal registry / PyPI mirror.
ARG BASE_IMAGE=registry.access.redhat.com/ubi9/python-311:latest
FROM ${BASE_IMAGE}

# Apply the latest OS security errata (patches base-image CVEs flagged by the
# image scan) and refresh pip's build tooling (setuptools/pip CVEs). Runs as
# root; the runtime drops back to the non-root UBI user below.
USER 0
RUN dnf -y update && dnf -y clean all \
    && python3 -m pip install --no-cache-dir --upgrade pip setuptools wheel

ARG PIP_INDEX_URL
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install the app and its runtime deps from pyproject.toml — the single source
# of truth for dependencies (no separate requirements.txt).
COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir .

EXPOSE 8080
# Non-root (UBI default user 1001).
USER 1001

# ENTRYPOINT fixes the server binary + app; CMD holds the default runtime flags
# so they can be overridden (e.g. --port, --workers) without replacing the
# entrypoint. Both exec-form so signals reach uvicorn (clean shutdown).
ENTRYPOINT ["python", "-m", "app.main"]