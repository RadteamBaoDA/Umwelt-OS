#!/bin/sh
# Alertmanager preflight (busybox sh). Fails closed when the severity=page webhook URL file is missing/empty or is
# the same URL as the ticket file, so an outage page can never silently land in the low-urgency ticket channel.
# ALERT_ALLOW_SHARED_PAGE_URL=true downgrades the identical-URL case to a loud warning (local/dev use only).
# Paths are overridable (TICKET_FILE/PAGE_FILE) so the check can be tested without the compose stack.
TICKET_FILE="${TICKET_FILE:-/run/secrets/alert_webhook_url}"
PAGE_FILE="${PAGE_FILE:-/run/secrets/alert_page_webhook_url}"
if [ ! -s "$PAGE_FILE" ]; then
  echo "FATAL: page webhook URL file $PAGE_FILE is missing or empty; set ALERT_PAGE_WEBHOOK_URL_FILE." >&2
  exit 1
fi
if [ -s "$TICKET_FILE" ] && cmp -s "$TICKET_FILE" "$PAGE_FILE"; then
  if [ "${ALERT_ALLOW_SHARED_PAGE_URL:-}" = "true" ]; then
    echo "WARNING: page and ticket alerts share one webhook URL; outage pages are not distinguishable. Dev only." >&2
  else
    echo "FATAL: ALERT_PAGE_WEBHOOK_URL_FILE equals ALERT_WEBHOOK_URL_FILE. Use a distinct paging endpoint, or set ALERT_ALLOW_SHARED_PAGE_URL=true to accept this (dev only)." >&2
    exit 1
  fi
fi
[ -n "${PREFLIGHT_ONLY:-}" ] && exit 0
exec /bin/alertmanager "$@"
