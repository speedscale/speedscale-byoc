# Changelog

## [1.2.0] - 2026-09-22

- Make the persistent delivery queue optional and disabled by default so installations do not require a PersistentVolumeClaim.
- Set `delivery.persistence.enabled=true` before upgrading from 1.1.0 to retain its PVC-backed queue.

## [1.1.0] - 2026-09-17

- Add a persistent queue with indefinite archive retries, rollout checksums, pod hardening, and restricted OTLP ingress.

## [1.0.0] - 2026-09-16

- Split the native GCS archive and reader from the former combined GCS and Datadog chart.
- Export only to Google Cloud Storage.
