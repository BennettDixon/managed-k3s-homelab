# Built and pushed from the workbench (spec §8); deploys are image-tag bumps.
FROM node:24-slim AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY tsconfig.json ./
COPY src ./src
RUN npm run build && npm prune --omit=dev

FROM node:24-slim
RUN useradd -r -u 10001 jobs
WORKDIR /app
COPY --from=build /app/node_modules ./node_modules
COPY --from=build /app/dist ./dist
COPY package.json ./
USER 10001
ENV NODE_ENV=production
EXPOSE 8080
CMD ["node", "dist/index.js"]
