FROM node:20-bookworm-slim

ENV NODE_ENV=production \
    APP_VERSION=1.6.2 \
    PORT=10000

WORKDIR /app

# SECTOR FORECAST v1.6.2 release archive.
# IMPORTANT: this file intentionally references ONLY the 162 archive.
COPY kr_market_news_radar_SECTOR_FORECAST_162.bin /tmp/app.tar.gz

RUN set -eux; \
    test -s /tmp/app.tar.gz; \
    tar -xzf /tmp/app.tar.gz -C /app; \
    rm -f /tmp/app.tar.gz; \
    test -f /app/package.json; \
    test -f /app/server.js

RUN npm install --omit=dev --no-audit --no-fund \
    && npm cache clean --force

EXPOSE 10000
CMD ["node", "server.js"]
