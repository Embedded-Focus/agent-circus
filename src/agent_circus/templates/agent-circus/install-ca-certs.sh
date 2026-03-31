#!/bin/bash
set -euo pipefail
# Copy forwarded CA certificates into the system store and refresh trust.
# Runs as root via sudo from docker-entrypoint.sh.
for f in /run/ca-host/*.crt; do
    [[ -f "$f" ]] || continue
    install -m 644 "$f" "/usr/local/share/ca-certificates/$(basename "$f")"
done
update-ca-certificates
