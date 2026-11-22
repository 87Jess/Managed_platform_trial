# Kong OSS, DB-less. Routes /v1, /healthz and /readyz to the policy broker,
# which runs as a sidecar container on 127.0.0.1:8081 in the same Cloud Run instance.
FROM kong:3.9.1
COPY gateway/kong.yml /kong/kong.yml
ENV KONG_DATABASE=off \
    KONG_DECLARATIVE_CONFIG=/kong/kong.yml \
    KONG_PROXY_LISTEN="0.0.0.0:8080" \
    KONG_ADMIN_LISTEN=off \
    KONG_PROXY_ACCESS_LOG=off \
    KONG_ADMIN_ACCESS_LOG=off \
    KONG_PROXY_ERROR_LOG=/dev/stderr \
    KONG_HEADERS=off
EXPOSE 8080
