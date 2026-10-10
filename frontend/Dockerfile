FROM nginxinc/nginx-unprivileged:stable-alpine
LABEL org.opencontainers.image.source="https://github.com/bombomteam/prohodnaya"

# Static files stay root-owned: the nginx user (101) can read but not modify them.
# config.js is not copied: the entrypoint generates it in /tmp at startup.
COPY index.html styles.css /usr/share/nginx/html/
COPY src/ /usr/share/nginx/html/src/
COPY docker/default.conf /etc/nginx/conf.d/default.conf
COPY docker/security-headers.conf /etc/nginx/snippets/security-headers.conf
COPY --chmod=755 docker/entrypoint.sh /usr/local/bin/prohodnaya-entrypoint

USER 101:101
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD wget -q -O /dev/null http://127.0.0.1:8080/healthz || exit 1

ENTRYPOINT ["/usr/local/bin/prohodnaya-entrypoint"]
