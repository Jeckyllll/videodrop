#!/bin/sh
# Apply only to VideoDrop's dedicated bridge; leave all other applications alone.
set -eu
iptables -N VIDEODROP_EGRESS 2>/dev/null || true
iptables -F VIDEODROP_EGRESS
iptables -A VIDEODROP_EGRESS -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
for network in 0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 169.254.0.0/16 172.16.0.0/12 192.0.0.0/24 192.168.0.0/16 198.18.0.0/15 224.0.0.0/4 240.0.0.0/4; do
  iptables -A VIDEODROP_EGRESS -d "$network" -j REJECT
done
iptables -A VIDEODROP_EGRESS -j RETURN
iptables -C DOCKER-USER -i br-videodrop -j VIDEODROP_EGRESS 2>/dev/null || iptables -I DOCKER-USER 1 -i br-videodrop -j VIDEODROP_EGRESS
iptables -C INPUT -i br-videodrop -m conntrack --ctstate NEW -j REJECT 2>/dev/null || iptables -I INPUT 1 -i br-videodrop -m conntrack --ctstate NEW -j REJECT
