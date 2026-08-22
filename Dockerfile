FROM node:20-alpine
WORKDIR /app
ENV NODE_ENV=production \
    APP_VERSION=1.9.9-H3F8 \
    GMIR_ALLOWED_ORIGINS=https://bbblaprk-svg.github.io \
    LS_OPENAPI_ENABLED=true \
    LS_BASE_URL=https://openapi.ls-sec.co.kr:8080 \
    LS_REALTIME_WS_URL=wss://openapi.ls-sec.co.kr:9443/websocket \
    MIN_MARKET_CAP_EOK=500 \
    BROAD_SCAN_MS=60000 \
    RANK_REORDER_MS=60000 \
    RANK_SCORE_MARGIN=3 \
    LS_FEED_STALE_MS=20000 \
    LS_REALTIME_RETAIN_MS=720000 \
    STORE_DIR=/tmp/gmir-lead20
RUN addgroup -S nodejs && adduser -S radar -G nodejs \
    && mkdir -p /tmp/gmir-lead20 /tmp/build-bin \
    && chown -R radar:nodejs /tmp/gmir-lead20 /app /tmp/build-bin
COPY GLOBAL_MARKET_IMPACT_RADAR_RENDER_LS_199_H3F8_LEAD20_REALTIME.bin /tmp/app.tar.gz
RUN set -eux; \
    tar -xzf /tmp/app.tar.gz -C /app; \
    rm -f /tmp/app.tar.gz; \
    npm install --omit=dev --no-audit --no-fund; \
    npm run check; \
    chown -R radar:nodejs /app
USER radar
EXPOSE 10000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 CMD wget -qO- "http://127.0.0.1:${PORT:-10000}/api/health" >/dev/null || exit 1
CMD ["node", "server.js"]
