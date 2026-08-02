FROM node:20-alpine
WORKDIR /app
ENV NODE_ENV=production \
    PORT=3000 \
    APP_VERSION=1.2.2 \
    STORE_DIR=/tmp/kr-news-radar \
    OFFICIAL_ONLY_FREE_MODE=true \
    OFFICIAL_FEEDS_ENABLED=true \
    WEEKEND_PAUSE=true \
    ACCESS_REFRESH_ENABLED=true \
    ACCESS_REFRESH_MIN_MS=600000 \
    ACCESS_REFRESH_MAX_SOURCES=6 \
    WEEKEND_VIEW_HOURS=72 \
    LOW_DUTY_WINDOWS=true \
    OFFICIAL_TICK_FAST_MS=30000 \
    OFFICIAL_TICK_MARKET_MS=60000 \
    OFFICIAL_TICK_QUIET_MS=180000 \
    SMART_MONEY_THRESHOLD=65 \
    SMART_NEWS_WINDOW_HOURS=8 \
    SEMIAI_NEWS_WINDOW_HOURS=12 \
    IMPORTANT_NEWS_WINDOW_HOURS=12 \
    MAX_INGEST_AGE_HOURS=72 \
    NEWS_MAX_ITEMS=1200 \
    NEWS_RETENTION_DAYS=3 \
    SSE_MAX_CONNECTIONS=5 \
    BACKUP_SCHEDULE_ENABLED=false
RUN addgroup -S nodejs && adduser -S radar -G nodejs \
    && mkdir -p /tmp/kr-news-radar \
    && chown -R radar:nodejs /tmp/kr-news-radar /app
COPY kr_market_news_radar_FREE_OFFICIAL_122.bin /tmp/app.tar.gz
RUN tar -xzf /tmp/app.tar.gz -C /app \
    && rm /tmp/app.tar.gz \
    && npm run check \
    && chown -R radar:nodejs /app
USER radar
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 CMD wget -qO- http://127.0.0.1:3000/api/health >/dev/null || exit 1
CMD ["node", "server.js"]
