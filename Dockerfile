FROM node:20-alpine
WORKDIR /app
ENV NODE_ENV=production \
    PORT=3000 \
    APP_VERSION=1.6.7 \
    STORE_DIR=/tmp/kr-news-radar \
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
    && mkdir -p /tmp/kr-news-radar \
    && chown -R radar:nodejs /tmp/kr-news-radar /app
COPY kr_market_news_radar_SECTOR_FORECAST_167.bin /tmp/app.tar.gz
RUN set -eux; \
    tar -xzf /tmp/app.tar.gz -C /app; \
    rm /tmp/app.tar.gz; \
    npm run check; \
    chown -R radar:nodejs /app
USER radar
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 CMD wget -qO- http://127.0.0.1:3000/api/health >/dev/null || exit 1
CMD ["node", "server.js"]
