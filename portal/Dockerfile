# Multi-stage build producing Next.js' standalone server (no runtime npm install,
# small final image) - suited to the airgapped registry/mirror. The npm registry
# is parameterised (ARG NPM_CONFIG_REGISTRY) exactly like the Serverless API's
# PIP_INDEX_URL, so builds point at the internal mirror.

# ---- deps: install node_modules against the (mirror) registry ----
FROM node:22-slim AS deps
ARG NPM_CONFIG_REGISTRY
ENV NPM_CONFIG_REGISTRY=${NPM_CONFIG_REGISTRY}
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci

# ---- build: compile the standalone Next.js output ----
FROM node:22-slim AS build
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

# ---- runtime: minimal image running server.js as non-root ----
FROM node:22-slim AS runtime
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORTAL_PORT=3000 \
    PORT=3000 \
    HOSTNAME=0.0.0.0
# The standalone output bundles only the server + pruned node_modules.
COPY --from=build /app/.next/standalone ./
COPY --from=build /app/.next/static ./.next/static
COPY --from=build /app/public ./public
EXPOSE 3000
USER 1001
ENTRYPOINT ["node", "server.js"]
