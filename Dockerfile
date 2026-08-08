FROM node:20-bookworm-slim
ENV NODE_ENV=production \
    PORT=10000 \
    APP_VERSION=1.6.4
WORKDIR /app
COPY kr_market_news_radar_SECTOR_FORECAST_164.bin /tmp/app.tar.gz
RUN tar -xzf /tmp/app.tar.gz -C /app \
    && rm /tmp/app.tar.gz \
    && test -f /app/package.json \
    && test -f /app/server.js \
    && npm install --omit=dev --no-audit --no-fund \
    && chown -R node:node /app
USER node
EXPOSE 10000
CMD ["node", "server.js"]
