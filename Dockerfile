FROM node:20-alpine
WORKDIR /app
ENV NODE_ENV=production \
    APP_VERSION=1.9.27-H3F26-NXT-PRE-HANDOFF-RECOVERY \
    GMIR_ALLOWED_ORIGINS=https://bbblaprk-svg.github.io \
    LS_OPENAPI_ENABLED=true \
    LS_BASE_URL=https://openapi.ls-sec.co.kr:8080 \
    LS_REALTIME_WS_URL=wss://openapi.ls-sec.co.kr:9443/websocket \
    MIN_MARKET_CAP_EOK=500 \
    BROAD_SCAN_MS=60000 \
    ACTIVE_POOL_MAX=600 \
    FAST_BOOT_ACTIVITY_MAX=100 \
    MARKET_ROTATION_BATCH=120 \
    LIVE_UNIFIED_ENRICH_MAX=36 \
    NXT_MOVER_MAX=16 \
    CAP_NEUTRAL_REVIEW_MAX=55 \
    ACTIVITY_BASELINE_ALPHA=0.18 \
    COLD_CLOSE_SWEEP_MAX=240 \
    COLD_CLOSE_POOL=40 \
    COLD_CLOSE_MIN_ROWS=20 \
    COLD_DAILY_REVIEW_MAX=55 \
    RANK_REORDER_MS=60000 \
    RANK_SCORE_MARGIN=3 \
    RANK_CONFIRM_MS=30000 \
    RANK_ROW_RETAIN_MS=300000 \
    RANK_MIN_HOLD_MS=180000 \
    RANK_MOVE_MARGIN=1.5 \
    RANK_MAX_MOVE_STEPS=3 \
    RANK_EMA_ALPHA=0.25 \
    SMART_FLOW_CONFIRM_MS=30000 \
    INVESTOR_FLOW_REFRESH_MS=120000 \
    INVESTOR_FLOW_STALE_MS=900000 \
    INVESTOR_FLOW_QUEUE_MAX=60 \
    CLOSE_RETRY_MS=30000 \
    ENGINE_TICK_MS=2000 \
    ENTRY_DATA_CONF_MIN=58 \
    ENTRY_FEED_COVERAGE_MIN=45 \
    ENTRY_PACKET_MAX_MS=20000 \
    NXT_EDGE_MAX_ROWS=10 \
    NXT_EARLY_CONFIRM_TICKS=2 \
    NXT_BUY_CONFIRM_TICKS=2 \
    NXT_EARLY_CONFIRM_MS=10000 \
    NXT_BUY_CONFIRM_MS=15000 \
    NXT_EDGE_ARM_MIN=58 \
    NXT_EDGE_EARLY_MIN=68 \
    NXT_EDGE_BUY_MIN=76 \
    NXT_EDGE_OVERNIGHT_MIN=80 \
    NXT_EDGE_BUY_MAX_PREMIUM=5.5 \
    NXT_EDGE_CHASE_PREMIUM=7 \
    NXT_EDGE_HARD_STOP_PCT=2.0 \
    NXT_EDGE_TRAIL_START_PCT=3.0 \
    NXT_EDGE_TRAIL_GIVEBACK_PCT=1.8 \
    CLOSEBET_STAGE_MIN_HOLD_MS=300000 \
    IGNITION_LOG_MAX=100 \
    PERFORMANCE_LOG_MAX=2000 \
    LS_FEED_STALE_MS=20000 \
    LS_UNIFIED_FALLBACK_RESETS=2 \
    LS_REALTIME_RETAIN_MS=720000 \
    VAPID_SUBJECT=mailto:gmir-alert@example.com
RUN addgroup -S nodejs && adduser -S radar -G nodejs \
    && mkdir -p /app/.state/gmir-lead20 /tmp/build-bin \
    && chown -R radar:nodejs /app /tmp/build-bin
COPY GLOBAL_MARKET_IMPACT_RADAR_RENDER_LS_1927_H3F26_NXT_PRE_HANDOFF_RECOVERY.bin /tmp/app.tar.gz
RUN set -eux; \
    tar -xzf /tmp/app.tar.gz -C /app; \
    rm -f /tmp/app.tar.gz; \
    npm install --omit=dev --no-audit --no-fund; \
    export STORE_DIR=/tmp/build-bin/test-state; \
    rm -rf /tmp/build-bin/test-state; mkdir -p /tmp/build-bin/test-state; \
    npm run check; \
    node scripts/check-cold.js; \
    node scripts/verify10.js; \
    node scripts/verify-boot.js; \
    node scripts/verify-fast-market.js; \
    node scripts/verify-session.js; \
    node scripts/verify-cap-failure.js; \
    node scripts/verify-root-cause-e2e.js; \
    node scripts/verify-nxt-native.js; \
    node scripts/verify-nxt-wire-key.js; \
    node scripts/verify-nxt-edge-final.js; \
    node scripts/verify-nxt-close-bet.js; \
    node scripts/verify-h3f25-precision.js; \
    node scripts/verify-feed-contract.js; \
    node scripts/verify-h3f25-ui.js; \
    node scripts/verify-h3f26-handoff.js; \
    rm -rf /tmp/build-bin/test-state; \
    rm -rf /app/.state/gmir-lead20/*; \
    chown -R radar:nodejs /app
USER radar
EXPOSE 10000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 CMD wget -qO- "http://127.0.0.1:${PORT:-10000}/api/health" >/dev/null || exit 1
CMD ["node", "server.js"]
