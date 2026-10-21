FROM node:24.20.0-alpine3.23 AS dependencies
WORKDIR /app
COPY package.json package-lock.json ./
COPY apps/web/package.json apps/web/package.json
RUN npm ci

FROM node:24.20.0-alpine3.23 AS build
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1 API_INTERNAL_URL=http://api:8000
COPY --from=dependencies /app/node_modules ./node_modules
COPY package.json package-lock.json ./
COPY apps/web ./apps/web
COPY docs/legal ./docs/legal
COPY docs/third-party-services.md ./docs/third-party-services.md
# npm nests workspace-only packages (e.g. @vitejs/plugin-react, type-checked via vitest.config.ts) under
# apps/web/node_modules; the root copy above does not include them.
COPY --from=dependencies /app/apps/web/node_modules ./apps/web/node_modules
RUN npm run build --workspace apps/web

FROM node:24.20.0-alpine3.23 AS runtime
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 HOSTNAME=0.0.0.0 PORT=3000
WORKDIR /app
RUN addgroup -S -g 10001 bbd && adduser -S -u 10001 -G bbd bbd
COPY --from=build --chown=bbd:bbd /app/apps/web/.next/standalone ./
COPY --from=build --chown=bbd:bbd /app/apps/web/.next/static ./apps/web/.next/static
# The public policy loader reads canonical Markdown at runtime, including standalone starts from /app.
COPY --from=build --chown=bbd:bbd /app/docs/legal ./docs/legal
COPY --from=build --chown=bbd:bbd /app/docs/third-party-services.md ./docs/third-party-services.md
# Standalone output omits public/: the vendored Generative UI modules (/vendor/genui/) live there.
COPY --from=build --chown=bbd:bbd /app/apps/web/public ./apps/web/public
USER bbd
EXPOSE 3000
CMD ["node", "apps/web/server.js"]
