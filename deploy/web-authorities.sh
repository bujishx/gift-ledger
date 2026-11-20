#!/bin/sh
# The nginx entrypoint runs this after Compose injects the two entry addresses.
set -eu

fail() {
    echo "GiftLedger: invalid $1 in the site HTTPS settings" >&2
    exit 1
}

valid_port() {
    case "$1" in ''|*[!0-9]*|0*) return 1;; esac
    [ "${#1}" -le 5 ] && [ "$1" -le 65535 ]
}

write_authority() {
    # Browsers normally omit the default port; accept explicit :443 as well.
    if [ "$2" = 443 ]; then printf '"%s" %s;\n' "$1" "$3"; fi
    printf '"%s:%s" %s;\n' "$1" "$2" "$3"
}

lan_host=${GIFTLEDGER_SITE_IP:?missing site IPv4 address}
lan_port=${GIFTLEDGER_HTTPS_PORT:?missing site HTTPS port}
# prepare checks the IPv4 address. Whitelist characters before emitting nginx configuration.
case "$lan_host" in *[!0-9.]*) fail GIFTLEDGER_SITE_IP;; esac
valid_port "$lan_port" || fail GIFTLEDGER_HTTPS_PORT

origin=${GIFT_LEDGER_REVERSE_PROXY_ORIGIN:-}
if [ -n "$origin" ]; then
    case "$origin" in https://*) ;; *) fail GIFTLEDGER_REVERSE_PROXY_ORIGIN;; esac
    authority=${origin#https://}
    authority=${authority%/}
    case "$authority" in
        *:*) domain=${authority%:*}; port=${authority##*:};;
        *) domain=$authority; port=443;;
    esac
    valid_port "$port" || fail GIFTLEDGER_REVERSE_PROXY_ORIGIN
    case "$domain" in ''|*[!a-zA-Z0-9.-]*) fail GIFTLEDGER_REVERSE_PROXY_ORIGIN;; esac
    if ! printf '%s\n' "$domain" | LC_ALL=C awk -F. '
        NF < 2 || length($0) > 253 || $NF !~ /[A-Za-z]/ {exit 1}
        {for (i=1; i<=NF; i++) if (length($i)==0 || length($i)>63 || $i ~ /^-/ || $i ~ /-$/) exit 1}
    '; then
        fail GIFTLEDGER_REVERSE_PROXY_ORIGIN
    fi
fi

{
    write_authority "$lan_host" "$lan_port" lan
    if [ -n "$origin" ]; then write_authority "$domain" "$port" public; fi
} > /tmp/giftledger-authorities.conf
