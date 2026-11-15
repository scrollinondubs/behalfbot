# behalfbot-installer SSH container. Holds no secrets: the per-install key
# and any stdin arrive with each request from that install's DO.
FROM node:22-alpine
RUN apk add --no-cache openssh-client \
  && adduser -D -s /sbin/nologin box
# Whole directory, not one file: server.mjs imports ./ssh.mjs (see the
# executor Dockerfile for what a single-file COPY cost there).
COPY container/ /app/
USER box
EXPOSE 8080
CMD ["node", "/app/server.mjs"]
