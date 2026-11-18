FROM kong:3.9.1
COPY gateway/kong.yml /etc/kong/kong.yml
ENV KONG_DATABASE=off \
    KONG_DECLARATIVE_CONFIG=/etc/kong/kong.yml \
    KONG_PROXY_LISTEN=0.0.0.0:8080 \
    KONG_ADMIN_LISTEN=off \
    KONG_STATUS_LISTEN=off \
    KONG_PROXY_ACCESS_LOG=off \
    KONG_PROXY_ERROR_LOG=/dev/stderr \
    KONG_LOG_LEVEL=warn
EXPOSE 8080