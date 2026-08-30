FROM node:22.23.2-alpine3.24 AS build

ENV NPM_CONFIG_AUDIT=false \
    NPM_CONFIG_FUND=false \
    NPM_CONFIG_UPDATE_NOTIFIER=false

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY tsconfig.json vite.config.ts index.html ./
COPY nginx.conf ./
COPY src ./src
COPY public ./public

RUN npm run typecheck \
    && npm test \
    && npm run build


FROM nginx:1.30.4-alpine3.24-slim

RUN apk upgrade --no-cache \
    && rm -f /etc/nginx/conf.d/default.conf \
    && mkdir -p /tmp/nginx/client_temp /tmp/nginx/proxy_temp \
    && mkdir -p /etc/nginx/tls \
    && chmod 0755 /etc/nginx/tls \
    && chown -R nginx:nginx /tmp/nginx /usr/share/nginx/html

COPY nginx.conf /etc/nginx/nginx.conf
COPY --from=build --chown=nginx:nginx /app/dist /usr/share/nginx/html

USER nginx:root

EXPOSE 8443

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -q -O /dev/null --no-check-certificate https://127.0.0.1:8443/healthz || exit 1

ENTRYPOINT []
CMD ["nginx", "-g", "daemon off;"]
