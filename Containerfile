# Airgap-friendly image. Base + pip index are mirrored internally (docs §9);
# build args let the pipeline point at the internal registry / PyPI mirror.
#
# The official python:3.13-slim image is rebuilt on Debian security updates, so
# it stays current without an in-build OS upgrade step; the CI scan gates on
# CRITICAL and reports HIGH.
ARG BASE_IMAGE=python:3.13-slim
FROM ${BASE_IMAGE}

ARG PIP_INDEX_URL
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install the app and its runtime deps from pyproject.toml — the single source
# of truth for dependencies (no separate requirements.txt).
COPY pyproject.toml ./
COPY app ./app
RUN pip install . \
    # OpenShift runs the container with an arbitrary UID in the root group;
    # make the workdir group-writable so that UID can operate here.
    && chgrp -R 0 /app && chmod -R g=u /app

EXPOSE 8080
# Non-root by default; OpenShift overrides the UID (must stay in group 0).
USER 1001

ENTRYPOINT ["python", "-m", "app.main"]
