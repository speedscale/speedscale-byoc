# Changelog

## [1.2.0] - 2026-09-17

- Add configurable sending queues and retries, rollout checksums, pod hardening, and restricted OTLP ingress.

## [1.1.0] - 2026-09-17

- Correlate RRPair logs with W3C trace context and workload service names.
- Mark HTTP 5xx spans and exception events as errors.
- Convert cumulative application metrics to delta temporality for Dynatrace.
- Bound retained conversion state for abandoned metric streams.

## [1.0.0] - 2026-09-16

- Add a dedicated Dynatrace collector for OTLP logs, traces, and metrics.
- Read the API token from a Kubernetes Secret.
