ARG BASE_IMAGE=python:3.13-slim
FROM ${BASE_IMAGE}

ARG PIP_INDEX_URL
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml ./
# Must land in ./app so setuptools finds the `app` package; `COPY app .` would
# flatten the contents into /app and `pip install .` would install nothing.
COPY app ./app
RUN pip install .
RUN chown -R 1001:0 /app && \
    chmod -R g=u /app

EXPOSE 8080
USER 1001

ENTRYPOINT ["python", "-m", "app.main"]
