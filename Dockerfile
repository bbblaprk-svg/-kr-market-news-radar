FROM node:22-alpine
WORKDIR /app
LABEL gmir.version="2.4.1" gmir.build_id="GMIR-2401-LOCKED-NEVER-BLANK-20260817"
ENV NODE_ENV=production \
    APP_VERSION=2.4.1 \
    APP_BUILD_ID=GMIR-2401-LOCKED-NEVER-BLANK-20260817 \
    PRE_SWING_ENABLED=true \
    PRE_SWING_MIN_MARKET_CAP_KRW=500000000000 \
    PRE_SWING_MIN_AVG_AMOUNT_KRW=5000000000 \
    PRE_SWING_HISTORY_DAYS=22 \
    PRE_SWING_HISTORY_MIN_DAYS=15 \
    PRE_SWING_TOP_N=10 \
    STORE_DIR=/tmp/kr-news-radar \
    RENDER_FREE_MODE=true
RUN addgroup -S nodejs && adduser -S radar -G nodejs && mkdir -p /tmp/kr-news-radar && chown -R radar:nodejs /tmp/kr-news-radar /app
COPY GLOBAL_MARKET_IMPACT_RADAR_2401_LOCKED.bin /tmp/app.tar.gz
RUN set -eux; \
    tar -xzf /tmp/app.tar.gz -C /app; rm -f /tmp/app.tar.gz; \
    test -f /app/server.js; test -f /app/lib/preSwingEngine.js; test -f /app/bootstrap/pre-swing-bootstrap.json; \
    node --check /app/server.js; node --check /app/lib/preSwingEngine.js; node --check /app/public/app.js; \
    node -e "const p=require('/app/lib/preSwingEngine');const s=p.snapshot({kick:false});if(!Array.isArray(s.items)||s.items.length!==10)throw new Error('PRE_SWING_NOT_10_AT_COLD_START');console.log('PRE_SWING_COLD_START_OK',s.items.length,s.anchorDate,s.historyDays);"; \
    chown -R radar:nodejs /app
USER radar
EXPOSE 10000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 CMD wget -qO- "http://127.0.0.1:${PORT:-10000}/api/health" >/dev/null || exit 1
CMD ["node","server.js"]
