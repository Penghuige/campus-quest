#!/usr/bin/env bash
# Generate the e2e-only MinIO TLS certificate (self-signed, SAN localhost).
#
# Why this exists (owner ruling 2026-10-08): the P3-B https e2e stack
# serves pages over https, so the backend-issued presigned upload URLs
# must ALSO be https — a browser blocks an http://localhost:9000 fetch
# from an https page (mixed content). The shared campusquest-minio-1
# stays http (the owner's local stack and every other test suite target
# it); the e2e-only instance (compose profile "e2e", published on
# localhost:9002) serves TLS with THIS certificate.
#
# Browser side needs no trust import: the e2e Playwright contexts run
# with ignoreHTTPSErrors (the same tolerance the P3-B page cert uses).
# Backend side trusts it via AWS_CA_BUNDLE pointing at public.crt
# (wired in playwright.config.ts / e2e/global-setup.ts) — no product
# code change.
#
# Idempotent: an existing certificate is left alone.
set -euo pipefail
cd "$(dirname "$0")/.."

cert_dir="e2e-certs/minio"
if [[ -f "$cert_dir/public.crt" && -f "$cert_dir/private.key" ]]; then
  echo "e2e minio certificate already present ($cert_dir)"
  exit 0
fi

mkdir -p "$cert_dir"
openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
  -keyout "$cert_dir/private.key" \
  -out "$cert_dir/public.crt" \
  -subj "/CN=localhost" \
  -addext "subjectAltName=DNS:localhost,DNS:minio-e2e,IP:127.0.0.1"

chmod 600 "$cert_dir/private.key"
echo "generated $cert_dir/public.crt (SAN: localhost, minio-e2e, 127.0.0.1)"
