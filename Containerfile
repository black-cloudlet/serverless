# Airgap-friendly image. Base + pip index are mirrored internally (docs §9);
# build args let the pipeline point at the internal registry / PyPI mirror.
ARG BASE_IMAGE=registry.access.redhat.com/ubi9/python-311:latest
FROM ${BASE_IMAGE}

ARG PIP_INDEX_URL
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8080
# Non-root (UBI default user 1001).
USER 1001

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
