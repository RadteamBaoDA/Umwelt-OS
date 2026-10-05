#!/bin/sh
set -eu

# Deny direct access to private/reserved networks; allow only established API replies,
# Docker DNS and public HTTP(S). URL checks in crawl.py also cover redirects/subresources.
iptables -P OUTPUT DROP
iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -A OUTPUT -o lo -j ACCEPT
iptables -A OUTPUT -d 127.0.0.11 -p udp --dport 53 -j ACCEPT
iptables -A OUTPUT -d 127.0.0.11 -p tcp --dport 53 -j ACCEPT
# Permit only the API's fixed internal callback address before the private-range drops.
iptables -A OUTPUT -d 172.29.250.10 -p tcp --dport 8000 -j ACCEPT
for range in 0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 169.254.0.0/16 172.16.0.0/12 192.0.0.0/24 192.168.0.0/16 198.18.0.0/15 224.0.0.0/4 240.0.0.0/4; do
  iptables -A OUTPUT -d "$range" -j DROP
done
iptables -A OUTPUT -p tcp -m multiport --dports 80,443 -j ACCEPT
ip6tables -P OUTPUT DROP
ip6tables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
ip6tables -A OUTPUT -o lo -j ACCEPT
ip6tables -A OUTPUT -d ::ffff:127.0.0.11 -p udp --dport 53 -j ACCEPT
ip6tables -A OUTPUT -d ::ffff:127.0.0.11 -p tcp --dport 53 -j ACCEPT
for range in ::/128 ::1/128 fc00::/7 fe80::/10 ff00::/8 2001:db8::/32; do
  ip6tables -A OUTPUT -d "$range" -j DROP
done
ip6tables -A OUTPUT -p tcp -m multiport --dports 80,443 -j ACCEPT

exec gosu bbd uvicorn modules.connectors.crawl:app --host 0.0.0.0 --port 8001
