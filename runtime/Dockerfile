FROM node:26.3.0-bookworm-slim

RUN apt-get update \
    && apt-get install --yes --no-install-recommends git python3 ripgrep ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev

ENTRYPOINT ["node", "/app/node_modules/promptfoo/dist/src/entrypoint.js"]
