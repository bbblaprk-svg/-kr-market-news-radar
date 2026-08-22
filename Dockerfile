FROM node:20-alpine
WORKDIR /app
ENV NODE_ENV=production \
    APP_VERSION=1.9.14-H3F13 \
    GMIR_ALLOWED_ORIGINS=https://bbblaprk-svg.github.io \
    LS_OPENAPI_ENABLED=true \
    LS_BASE_URL=https://openapi.ls-sec.co.kr:8080 \
    LS_REALTIME_WS_URL=wss://openapi.ls-sec.co.kr:9443/websocket \
    MIN_MARKET_CAP_EOK=500 \
    BROAD_SCAN_MS=60000 \
    ACTIVE_POOL_MAX=600 \
    UNIFIED_REGULAR_SWEEP_MAX=120 \
    UNIFIED_NXT_SWEEP_MAX=180 \
    RANK_REORDER_MS=60000 \
    RANK_SCORE_MARGIN=3 \
    RANK_CONFIRM_MS=30000 \
    RANK_ROW_RETAIN_MS=300000 \
    RANK_MIN_HOLD_MS=180000 \
    RANK_MOVE_MARGIN=1.5 \
    RANK_MAX_MOVE_STEPS=3 \
    RANK_EMA_ALPHA=0.25 \
    CLOSE_RETRY_MS=30000 \
    ENGINE_TICK_MS=2000 \
    NXT_EDGE_MAX_ROWS=5 \
    NXT_EDGE_ARM_MIN=58 \
    NXT_EDGE_EARLY_MIN=68 \
    NXT_EDGE_BUY_MIN=76 \
    NXT_EDGE_OVERNIGHT_MIN=80 \
    NXT_EDGE_BUY_MAX_PREMIUM=5.5 \
    NXT_EDGE_CHASE_PREMIUM=7 \
    NXT_EDGE_HARD_STOP_PCT=2.0 \
    NXT_EDGE_TRAIL_START_PCT=3.0 \
    NXT_EDGE_TRAIL_GIVEBACK_PCT=1.8 \
    LS_FEED_STALE_MS=20000 \
    LS_UNIFIED_FALLBACK_RESETS=2 \
    LS_REALTIME_RETAIN_MS=720000 \
    VAPID_SUBJECT=mailto:gmir-alert@example.com \
    STORE_DIR=/tmp/gmir-lead20
RUN addgroup -S nodejs && adduser -S radar -G nodejs \
    && mkdir -p /tmp/gmir-lead20 /tmp/build-bin \
    && chown -R radar:nodejs /tmp/gmir-lead20 /app /tmp/build-bin
COPY GLOBAL_MARKET_IMPACT_RADAR_RENDER_LS_1914_H3F13_FIXED20_CLOSE_HOLD.bin /tmp/app.tar.gz
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
