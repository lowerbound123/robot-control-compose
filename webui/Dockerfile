FROM node:24-bookworm-slim@sha256:c86ecead9e55a5210ac1727de38a4ca840bf03dcaf34dc9749addbf383b1920c AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
RUN npm run build

FROM nginx:stable-alpine@sha256:0e52bd054126cdc040b1276c0af347aeb02a41b9cfe305d6094767ac30e97949
COPY --from=build /app/dist/ /usr/share/nginx/html/
COPY nginx.conf.template /etc/nginx/rc.conf.template
COPY --chmod=755 entrypoint.sh /rc-entrypoint.sh
EXPOSE 8080
ENTRYPOINT ["/rc-entrypoint.sh"]
HEALTHCHECK --interval=5s --timeout=2s --retries=3 CMD wget -q -O /dev/null http://127.0.0.1:8080/monitor || exit 1
