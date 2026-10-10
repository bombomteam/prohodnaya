FROM quay.io/keycloak/keycloak:26.5.7

ENV KC_DB=dev-file \
    KC_HEALTH_ENABLED=true \
    KC_HTTP_ENABLED=true

COPY --chown=1000:0 import/ /opt/keycloak/project-import/
COPY --chown=1000:0 --chmod=0755 scripts/ /opt/keycloak/project-scripts/

USER 1000:0
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=5 \
    CMD ["bash", "/opt/keycloak/project-scripts/healthcheck.sh"]
ENTRYPOINT ["bash", "/opt/keycloak/project-scripts/entrypoint.sh"]
CMD ["start-dev", "--import-realm"]
