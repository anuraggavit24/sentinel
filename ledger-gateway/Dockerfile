# Read-only bridge to the fraud-intel ledger. Build, then run without dev dependencies or sources.
FROM node:22-slim AS build
WORKDIR /app
COPY package.json package-lock.json tsconfig.json ./
RUN npm ci --no-audit --no-fund
COPY src ./src
COPY test ./test
RUN npx tsc

FROM node:22-slim
WORKDIR /app
ENV NODE_ENV=production
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund && npm cache clean --force
COPY --from=build /app/dist/src ./dist/src
USER node
EXPOSE 8100
CMD ["node", "dist/src/index.js"]
