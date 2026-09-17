# Changelog

## [1.2.0] - 2026-09-17

- Populate readable RRPair log messages and HTTP attributes.
- Isolate log service attribution with `groupbyattrs/service`.
- Derive New Relic APM's `http.server.duration` metric from server spans.

## [1.1.0] - 2026-09-17

- Add configurable sending queues and retries, rollout checksums, pod hardening, and restricted OTLP ingress.

## [1.0.0] - 2026-09-17

- Add a dedicated New Relic collector for OTLP logs, traces, and metrics.
- Read the license key from a Kubernetes Secret.
