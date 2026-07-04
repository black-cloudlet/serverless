ARG BASE_IMAGE=python:3.13-slim
FROM ${BASE_IMAGE}

ARG PIP_INDEX_URL
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Install the app + its deps into site-packages from a throwaway build dir, then
# drop the source. The runtime imports `app` from site-packages, so the final
# image ships no source tree — no redundant /app/app, and nothing to keep in
# sync with the installed package.
WORKDIR /src
COPY pyproject.toml ./
COPY app ./app
RUN pip install . && rm -rf /src

WORKDIR /app
EXPOSE 8080
# Non-root; OpenShift may override the UID (site-packages is world-readable, so
# any UID can import the app).
USER 1001

ENTRYPOINT ["python", "-m", "app.main"]
