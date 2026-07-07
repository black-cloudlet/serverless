# Build stage: install the package (api + common) into an isolated prefix so the
# runtime image can copy just the result — no build tools, no source tree.
FROM python:3.13-slim AS build

ARG PIP_INDEX_URL
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
COPY pyproject.toml ./
COPY common ./common
COPY api ./api
RUN pip install --prefix=/install .

# Runtime stage: carry only the installed packages (into the base image's
# /usr/local). The app is imported from the installed package; there is no source
# tree in the image.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY --from=build /install /usr/local

EXPOSE 8080
USER 1001

ENTRYPOINT ["python", "-m", "api.main"]
