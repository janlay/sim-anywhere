#!/bin/sh
# Proxy the node's /sim-anywhere/ page and JSON API behind HA ingress.
# The page calls the API at the absolute path /cgi-bin/sim-anywhere-api/, which would
# escape the ingress prefix, so it is rewritten to a relative path.
set -e
HOST=$(jq -r .host /data/options.json)
cat > /tmp/nginx.conf <<CONF
worker_processes 1;
pid /tmp/nginx.pid;
error_log /dev/stderr warn;
events {}
http {
  access_log off;
  client_body_temp_path /tmp/body;
  proxy_temp_path /tmp/proxy;
  server {
    listen 8099;
    allow 172.30.32.2;
    deny all;
    add_header Cache-Control "no-store, no-cache, must-revalidate" always;
    proxy_set_header Accept-Encoding "";
    location /cgi-bin/sim-anywhere-api/ {
      proxy_cache off;
      proxy_buffering off;
      proxy_pass http://$HOST/cgi-bin/sim-anywhere-api/;
    }
    location / {
      proxy_cache off;
      proxy_pass http://$HOST/sim-anywhere/;
      sub_filter "'/cgi-bin/sim-anywhere-api/'" "'cgi-bin/sim-anywhere-api/'";
      sub_filter_once off;
    }
  }
}
CONF
exec nginx -c /tmp/nginx.conf -g 'daemon off;'
