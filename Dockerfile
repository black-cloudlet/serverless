FROM python:3.13-slim

ARG PIP_INDEX_URL
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Install into site-packages from a build dir, then discard the sources: the
# runtime image carries no source tree (and no nested app/app), and the app is
# imported from the installed package.
COPY pyproject.toml /src/
COPY app /src/app
RUN pip install /src && rm -rf /src

WORKDIR /
EXPOSE 8080
USER 1001

ENTRYPOINT ["python", "-m", "app.main"]
