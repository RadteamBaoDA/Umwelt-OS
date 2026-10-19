FROM alpine:3.22 AS firewall
RUN apk add --no-cache iptables su-exec
RUN mkdir -p /out/usr/sbin /out/usr/lib && \
    cp -aL /usr/sbin/iptables* /usr/sbin/ip6tables* /usr/sbin/xtables* /out/usr/sbin/ && \
    cp -aL /usr/lib/xtables /out/usr/lib/ && \
    mkdir -p /out/usr/local/bin && cp -aL /sbin/su-exec /out/usr/local/bin/su-exec
RUN for file in /usr/sbin/iptables /usr/sbin/ip6tables /usr/lib/xtables/*.so; do \
      ldd "$file" 2>/dev/null | awk '/=> \/|^\// { path = ($2 == "=>" ? $3 : $1); if (path ~ /^\//) print path }'; \
    done | sort -u | while read -r dependency; do \
      mkdir -p "/out$(dirname "$dependency")"; cp -aL "$dependency" "/out$dependency"; \
    done

FROM n8nio/n8n:2.5.2
USER root
COPY --from=firewall /out/ /
COPY docker/n8n-connectors-entrypoint.sh /usr/local/bin/n8n-connectors-entrypoint
RUN chmod 0755 /usr/local/bin/n8n-connectors-entrypoint && iptables --version && ip6tables --version
ENTRYPOINT ["/usr/local/bin/n8n-connectors-entrypoint"]
