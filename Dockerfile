FROM node:20-alpine
WORKDIR /app
ENV NODE_ENV=production \
    APP_VERSION=1.9.6 \
    GMIR_ALLOWED_ORIGINS=https://bbblaprk-svg.github.io \
    LS_OPENAPI_ENABLED=true \
    LS_NEWS_ENABLED=true \
    LS_NEWS_WS_URL=wss://openapi.ls-sec.co.kr:9443/websocket \
    LS_NEWS_TR_KEY=NWS001 \
    LS_DEEP_NEWS_ENABLED=true \
    LS_REALTIME_ENABLED=true \
    LS_REALTIME_WS_URL=wss://openapi.ls-sec.co.kr:9443/websocket \
    LS_REALTIME_STREAMS=US3,UH1,UPH \
    LS_REALTIME_MAX_TARGETS=12 \
    LS_REALTIME_TARGET_REFRESH_MS=15000 \
    LS_DEEP_NEWS_BODY_ENABLED=true \
    LS_DEEP_NEWS_MIN_IMPORTANCE=55 \
    LS_T3102_SCHEMA_PROBE=true \
    LS_T3102_MIN_INTERVAL_MS=1250 \
    LS_SNAPSHOT_CACHE_MS=12000 \
    LS_PREIGNITION_MAX_TARGETS=12 \
    LS_TAPE_ROWS=120 \
    LS_MARKET_SCAN_CACHE_MS=15000 \
    LS_CLOSED_MARKET_SCAN_CACHE_MS=1800000 \
    LS_CLOSED_DECISION_CACHE_MS=1800000 \
    LS_CLOSED_DECISION_TARGETS=12 \
    LS_MARKET_SCAN_SHORTLIST=24 \
    LS_MARKET_SCAN_MAX_UNIVERSE=320 \
    LS_MULTI_QUOTE_BATCH=50 \
    STORE_DIR=/tmp/kr-news-radar \
    RENDER_FREE_MODE=true \
    OFFICIAL_ONLY_FREE_MODE=true \
    OFFICIAL_FEEDS_ENABLED=true \
    FOREIGN_FIRST_MODE=true \
    ACTIVE_MONITOR_MAX=20 \
    AUTO_UNIVERSE_MAX=200 \
    SECTOR_ROTATION_WINDOW_HOURS=18 \
    WEEKEND_PAUSE=false \
    ACCESS_REFRESH_ENABLED=true \
    NEWS_VIEW_HOURS=24 \
    NEWS_RETENTION_DAYS=3 \
    SSE_MAX_CONNECTIONS=5 \
    BACKUP_SCHEDULE_ENABLED=false
RUN addgroup -S nodejs && adduser -S radar -G nodejs \
    && mkdir -p /tmp/kr-news-radar /tmp/build-bin \
    && chown -R radar:nodejs /tmp/kr-news-radar /app /tmp/build-bin
COPY GLOBAL_MARKET_IMPACT_RADAR_RENDER_LS_196_FAST_BOOT_AUTH.bin /tmp/app.tar.gz
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
