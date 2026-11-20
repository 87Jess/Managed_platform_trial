#!/bin/bash
# Startup script for the agent egress proxy VM (Debian 12).
# Allows HTTPS (CONNECT on port 443) only to the hosts listed in ALLOWED, and only from the
# Agent Engine PSC subnet. Everything else is denied. Change ALLOWED / PSC_RANGE as needed.
set -euo pipefail
ALLOWED="data360api.worldbank.org maimcpext.worldbank.org"
PSC_RANGE="10.10.1.0/28"

if ! command -v squid >/dev/null; then
  apt-get update -y
  DEBIAN_FRONTEND=noninteractive apt-get install -y squid
fi

cat > /etc/squid/squid.conf <<CONF
http_port 3128
acl agents src ${PSC_RANGE}
acl allowed_hosts dstdomain ${ALLOWED}
acl SSL_ports port 443
acl CONNECT method CONNECT
http_access deny CONNECT !SSL_ports
http_access allow agents allowed_hosts
http_access deny all
cache deny all
via off
forwarded_for delete
access_log daemon:/var/log/squid/access.log squid
CONF

systemctl enable squid
systemctl restart squid
