FROM nginxinc/nginx-unprivileged:stable-alpine
LABEL org.opencontainers.image.source="https://github.com/bombomteam/prohodnaya"

COPY --chown=101:101 index.html styles.css config.js /usr/share/nginx/html/
COPY --chown=101:101 src/ /usr/share/nginx/html/src/
COPY docker/default.conf /etc/nginx/conf.d/default.conf
COPY --chmod=755 docker/entrypoint.sh /usr/local/bin/prohodnaya-entrypoint

USER 101:101
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD wget -q -O /dev/null http://127.0.0.1:8080/healthz || exit 1

ENTRYPOINT ["/usr/local/bin/prohodnaya-entrypoint"]
