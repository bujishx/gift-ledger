FROM node:22-alpine AS build
WORKDIR /app
RUN npm install --global --no-audit --no-fund pnpm@11.19.0
COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY frontend/ ./
RUN pnpm run build

FROM nginx:stable-alpine
COPY deploy/nginx.conf /etc/nginx/conf.d/default.conf
COPY --chmod=755 deploy/web-authorities.sh /docker-entrypoint.d/19-giftledger-authorities.sh
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 443
