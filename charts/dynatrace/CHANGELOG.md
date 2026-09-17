# Changelog

## [1.1.0] - 2026-09-17

- Correlate RRPair logs with W3C trace context and workload service names.
- Mark HTTP 5xx spans and exception events as errors.
- Convert cumulative application metrics to delta temporality for Dynatrace.

## [1.0.0] - 2026-09-16

- Add a dedicated Dynatrace collector for OTLP logs, traces, and metrics.
- Read the API token from a Kubernetes Secret.
