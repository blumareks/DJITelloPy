#!/bin/sh
# Create a private CA and one TLS certificate per drone IP (IP is in the subject alternative names).
#
# Usage: fleet_ui/make_certs.sh [-o OUTPUT_DIR] IP [IP ...]
#   e.g. fleet_ui/make_certs.sh 192.168.4.11 192.168.4.12
#
# Output (default fleet_ui/certs/, ignored by git):
#   ca.pem                 CA certificate: give this to the UI ("CA bundle"); not secret
#   ca.key                 CA private key: keep on this machine only
#   <ip>/cert.pem, key.pem copy to the matching Pi and use as TLS_CERTFILE / TLS_KEYFILE
set -eu
umask 077

OUT="$(dirname "$0")/certs"
if [ "${1:-}" = "-o" ]; then
    [ $# -ge 2 ] || { echo "-o needs a directory" >&2; exit 2; }
    OUT="$2"
    shift 2
fi
[ $# -ge 1 ] || { echo "usage: $0 [-o OUTPUT_DIR] IP [IP ...]" >&2; exit 2; }

for ip in "$@"; do
    printf '%s' "$ip" | grep -Eq '^([0-9]{1,3}\.){3}[0-9]{1,3}$' \
        || { echo "not an IPv4 address: $ip" >&2; exit 2; }
done

mkdir -p "$OUT"
if [ ! -f "$OUT/ca.key" ]; then
    openssl ecparam -name prime256v1 -genkey -noout -out "$OUT/ca.key"
    openssl req -x509 -new -key "$OUT/ca.key" -sha256 -days 825 \
        -subj "/CN=Tello fleet private CA" \
        -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
        -addext "keyUsage=critical,keyCertSign,cRLSign" \
        -out "$OUT/ca.pem"
    echo "created CA: $OUT/ca.pem"
fi

for ip in "$@"; do
    dir="$OUT/$ip"
    mkdir -p "$dir"
    openssl ecparam -name prime256v1 -genkey -noout -out "$dir/key.pem"
    openssl req -new -key "$dir/key.pem" -subj "/CN=$ip" -out "$dir/req.csr"
    printf 'subjectAltName=IP:%s\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\n' \
        "$ip" > "$dir/ext.cnf"
    openssl x509 -req -in "$dir/req.csr" -CA "$OUT/ca.pem" -CAkey "$OUT/ca.key" -CAcreateserial \
        -sha256 -days 397 -extfile "$dir/ext.cnf" -out "$dir/cert.pem" 2>/dev/null
    rm -f "$dir/req.csr" "$dir/ext.cnf"
    echo "created certificate for $ip: $dir/cert.pem (valid 397 days; re-run to renew)"
done
