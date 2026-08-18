FROM node:22-alpine
WORKDIR /app
LABEL gmir.version="3.0.0" gmir.build_id="GMIR-300-GROUNDUP-MARKET-KERNEL-FINAL-20260818"
ENV NODE_ENV=production \
    APP_VERSION=3.0.0 \
    APP_BUILD_ID=GMIR-300-GROUNDUP-MARKET-KERNEL-FINAL-20260818 \
    PRE_SWING_ENABLED=true \
    PRE_SWING_MIN_MARKET_CAP_KRW=500000000000 \
    PRE_SWING_MIN_AVG_AMOUNT_KRW=5000000000 \
    PRE_SWING_HISTORY_DAYS=22 \
    PRE_SWING_HISTORY_MIN_DAYS=15 \
    PRE_SWING_TOP_N=10 \
    PRE_SWING_FLOW_WS_SLOTS=5 \
    PRE_SWING_FLOW_ROTATION_MS=120000 \
    PRE_SWING_FLOW_PROXY_MAX_AGE_MS=180000 \
    PRE_SWING_FLOW_PROXY_HISTORY_MS=900000 \
    PRE_SWING_FLOW_PROXY_MIN_MATURITY_SEC=45 \
    PRE_SWING_FLOW_PROXY_REFRESH_MS=60000 \
    PRE_SWING_T1717_DEADLINE_MS=20000 \
    LS_API_T1717_RPS=0.8 \
    LS_RUNTIME_CORE_WS_MAX=20 \
    LS_REALTIME_MAX_TARGETS=25 \
    LS_RUNTIME_RANK_TICK_MS=2000 \
    LS_RUNTIME_INGEST_TICK_MS=4000 \
    LS_RUNTIME_CORE_SUB_REFRESH_MS=12000 \
    LS_RUNTIME_T8450_FRESH_MS=7000 \
    LS_RUNTIME_REST_FRESH_MS=10000 \
    LS_REALTIME_FRESH_PACKET_MS=15000 \
    LS_REALTIME_ACK_ONLY_RECOVERY_MS=12000 \
    LS_REALTIME_RETAIN_MS=900000 \
    LS_REALTIME_BOOK_RETAIN_MS=900000 \
    KRX_OPERATOR_EXTRA_CLOSED_DATES_ENABLED=false \
    STORE_DIR=/tmp/kr-news-radar \
    RENDER_FREE_MODE=true
RUN addgroup -S nodejs && adduser -S radar -G nodejs && mkdir -p /tmp/kr-news-radar && chown -R radar:nodejs /tmp/kr-news-radar /app
COPY GLOBAL_MARKET_IMPACT_RADAR_300_GROUNDUP_FINAL.bin /tmp/app.tar.gz
RUN set -eux; \
    tar -xzf /tmp/app.tar.gz -C /app; rm -f /tmp/app.tar.gz; \
    test -f /app/server.js; \
    test -f /app/lib/runtimeCore.js; \
    test -f /app/lib/lsRealtimeMarket.js; \
    test -f /app/lib/preSwingEngine.js; \
    test -f /app/bootstrap/pre-swing-bootstrap.json; \
    grep -Fq "GMIR 3.0 Ground-Up Market Runtime Kernel" /app/lib/runtimeCore.js; \
    grep -Fq "ACK is never counted as market DATA" /app/lib/lsRealtimeMarket.js; \
    grep -Fq "gmir-shell-v300-groundup" /app/public/sw.js; \
    grep -Fq "./app.js?v=3000g" /app/public/index.html; \
    grep -Fq "v3.0.0" /app/public/index.html; \
    npm run check; \
    rm -rf /app/data /app/node_modules; mkdir -p /app/data; \
    chown -R radar:nodejs /app /tmp/kr-news-radar
USER radar
EXPOSE 10000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 CMD wget -qO- "http://127.0.0.1:${PORT:-10000}/api/health" >/dev/null || exit 1
CMD ["node","server.js"]
