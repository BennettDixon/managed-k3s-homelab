# Built and pushed from the workbench (spec §8); deploys are image-tag bumps.
# node:22 (not 24): better-sqlite3@11 ships no prebuilt binary for Node 24's
# ABI, and -slim has no toolchain for the node-gyp fallback — the 24 image
# cannot build. 22 matches engines (>=22) and local dev.
FROM node:22-slim AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY tsconfig.json ./
COPY src ./src
RUN npm run build && npm prune --omit=dev

FROM node:22-slim
RUN useradd -r -u 10001 jobs
WORKDIR /app
COPY --from=build /app/node_modules ./node_modules
COPY --from=build /app/dist ./dist
COPY package.json ./
USER 10001
ENV NODE_ENV=production
EXPOSE 8080
CMD ["node", "dist/index.js"]
