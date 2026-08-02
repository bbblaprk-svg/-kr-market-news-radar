FROM node:20-alpine
WORKDIR /app
ENV NODE_ENV=production \
    PORT=3000 \
    APP_VERSION=1.3.0 \
    STORE_DIR=/tmp/kr-news-radar \
    OFFICIAL_ONLY_FREE_MODE=true \
    OFFICIAL_FEEDS_ENABLED=true \
    FOREIGN_FIRST_MODE=true \
    FOREIGN_LEAD_MAX_STORIES=24 \
    EARLY_EDGE_ENABLED=true \
    EARLY_EDGE_MAX_ROWS=20 \
    NAVER_PUBLIC_ENABLED=true \
    NAVER_PUBLIC_INTERVAL_MS=600000 \
    WEEKEND_PAUSE=false \
    WEEKEND_TICK_MS=180000 \
    ACCESS_REFRESH_ENABLED=true \
    ACCESS_REFRESH_MIN_MS=300000 \
    ACCESS_REFRESH_MAX_SOURCES=24 \
    WEEKEND_VIEW_HOURS=48 \
    LOW_DUTY_WINDOWS=true \
    OFFICIAL_TICK_FAST_MS=30000 \
    OFFICIAL_TICK_MARKET_MS=60000 \
    OFFICIAL_TICK_QUIET_MS=180000 \
    SMART_MONEY_THRESHOLD=65 \
    SMART_NEWS_WINDOW_HOURS=48 \
    SEMIAI_NEWS_WINDOW_HOURS=48 \
    IMPORTANT_NEWS_WINDOW_HOURS=48 \
    MAX_INGEST_AGE_HOURS=48 \
    NEWS_VIEW_HOURS=48 \
    NEWS_MAX_ITEMS=900 \
    NEWS_RETENTION_DAYS=3 \
    AUTO_UNIVERSE_MAX=200 \
    SSE_MAX_CONNECTIONS=5 \
    BACKUP_SCHEDULE_ENABLED=false
RUN addgroup -S nodejs && adduser -S radar -G nodejs \
    && mkdir -p /tmp/kr-news-radar \
    && chown -R radar:nodejs /tmp/kr-news-radar /app
COPY kr_market_news_radar_WEEKEND48_130.bin /tmp/app.tar.gz
RUN tar -xzf /tmp/app.tar.gz -C /app \
    && rm /tmp/app.tar.gz \
    && npm run check \
    && chown -R radar:nodejs /app
USER radar
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 CMD wget -qO- http://127.0.0.1:3000/api/health >/dev/null || exit 1
CMD ["node", "server.js"]
