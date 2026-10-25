ARG BUILD_FROM=ghcr.io/home-assistant/base:latest
FROM ${BUILD_FROM}

RUN apk add --no-cache nginx

COPY run.sh /run.sh
CMD ["/run.sh"]
