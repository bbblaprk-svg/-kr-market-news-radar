FROM node:22-alpine
WORKDIR /app
LABEL gmir.version="2.4.5" gmir.build_id="GMIR-2405-NXT-SESSION-RECOVERY-FINAL-20260818"
ENV NODE_ENV=production \
    APP_VERSION=2.4.5 \
    APP_BUILD_ID=GMIR-2405-NXT-SESSION-RECOVERY-FINAL-20260818 \
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
    LS_RUNTIME_CORE_WS_CHALLENGERS=5 \
    LS_REALTIME_MAX_TARGETS=25 \
    LS_REALTIME_RETAIN_MS=900000 \
    LS_REALTIME_BOOK_RETAIN_MS=900000 \
    KRX_OPERATOR_EXTRA_CLOSED_DATES_ENABLED=false \
    STORE_DIR=/tmp/kr-news-radar \
    RENDER_FREE_MODE=true
RUN addgroup -S nodejs && adduser -S radar -G nodejs && mkdir -p /tmp/kr-news-radar && chown -R radar:nodejs /tmp/kr-news-radar /app
COPY GLOBAL_MARKET_IMPACT_RADAR_245_NXT_SESSION_RECOVERY_FINAL.bin /tmp/app.tar.gz
RUN set -eux; \
    tar -xzf /tmp/app.tar.gz -C /app; rm -f /tmp/app.tar.gz; \
    test -f /app/server.js; test -f /app/lib/marketSession.js; test -f /app/lib/krxCalendar.js; \
    test -f /app/lib/runtimeCore.js; test -f /app/lib/preSwingEngine.js; test -f /app/lib/preSwingFlowBridge.js; \
    test -f /app/public/sw.js; test -f /app/public/index.html; test -f /app/public/app.js; \
    node --check /app/server.js; node --check /app/lib/marketSession.js; node --check /app/lib/krxCalendar.js; node --check /app/lib/runtimeCore.js; \
    grep -Fq "gmir-shell-v245-nxt-session" /app/public/sw.js; \
    grep -Fq "./app.js?v=2405n" /app/public/index.html; \
    grep -Fq "v2.4.5" /app/public/index.html; \
    node /app/scripts/check-market-session-nxt.js; \
    node /app/scripts/check-v245-nxt-session-recovery.js; \
    node /app/scripts/check-pre-swing-flow-fusion.js; \
    node /app/scripts/check-pre-swing-flow-freshness.js; \
    node /app/scripts/check-pre-swing-flow-slots.js; \
    npm run check; \
    chown -R radar:nodejs /app
USER radar
EXPOSE 10000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 CMD wget -qO- "http://127.0.0.1:${PORT:-10000}/api/health" >/dev/null || exit 1
CMD ["node","server.js"]
