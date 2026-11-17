#!/bin/sh
set -eu

api_ip=${CONNECTOR_API_IP:-172.29.250.10}
iptables -P OUTPUT DROP
iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -A OUTPUT -o lo -j ACCEPT
iptables -A OUTPUT -d 127.0.0.11 -p udp --dport 53 -j ACCEPT
iptables -A OUTPUT -d 127.0.0.11 -p tcp --dport 53 -j ACCEPT
# This explicit API /32 exception must precede the broader private-range drops below.
iptables -A OUTPUT -d "$api_ip/32" -p tcp --dport 8000 -j ACCEPT
for range in 0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 169.254.0.0/16 172.16.0.0/12 192.0.0.0/24 192.0.2.0/24 192.88.99.0/24 192.168.0.0/16 198.18.0.0/15 198.51.100.0/24 203.0.113.0/24 224.0.0.0/4 240.0.0.0/4; do
  iptables -A OUTPUT -d "$range" -j DROP
done
iptables -A OUTPUT -p tcp -m multiport --dports 80,443 -j ACCEPT
# Keep IPv6 egress closed except for established replies, loopback, and the listed Docker DNS traffic.
ip6tables -P OUTPUT DROP
ip6tables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
ip6tables -A OUTPUT -o lo -j ACCEPT
ip6tables -A OUTPUT -d ::ffff:127.0.0.11 -p udp --dport 53 -j ACCEPT
ip6tables -A OUTPUT -d ::ffff:127.0.0.11 -p tcp --dport 53 -j ACCEPT

# Drop root (and NET_ADMIN) before n8n starts so workflow code cannot rewrite the firewall.
exec su-exec node /docker-entrypoint.sh "$@"
