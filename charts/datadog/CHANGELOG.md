# Changelog

## [1.3.0] - 2026-09-18

- Add source service, remote destination, protocol, command, and status attributes to RRPair logs.
- Preserve complete RRPair bodies for direct import by the Datadog-to-proxymock recipe.
- Isolate service attribution when one OTLP batch contains multiple workloads.

## [1.2.0] - 2026-09-17

- Add configurable sending queues and retries, rollout checksums, pod hardening, and restricted OTLP ingress.

## [1.1.0] - 2026-09-17

- Correlate RRPair logs with W3C trace context and workload service names.
- Mark HTTP 5xx spans and exception events as errors.
- Generate Datadog APM trace metrics through the Datadog connector.

## [1.0.0] - 2026-09-16

- Add a dedicated Datadog collector for OTLP logs, traces, and metrics.
- Read the API key from a Kubernetes Secret.
