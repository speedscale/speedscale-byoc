# Changelog

## [2.0.0] - 2026-09-17

- Add logs, traces, and metrics signal selection, custom headers, TLS and mTLS, persistent queues, resource controls, pod hardening, and restricted OTLP ingress.
- Add a metadata-only security profile with normalized network, HTTP, workload, DLP, and trace-correlation fields.
- Add Elastic Security, Falcon LogScale, and OpenSearch Security Analytics presets.
- Qualify Kubernetes resource names with the Helm release name.

## [1.2.0] - 2026-09-17

- Reserve New Relic for its dedicated logs, traces, and metrics chart.

## [1.1.0] - 2026-09-16

- Reserve Datadog and Dynatrace for their dedicated charts.
- Keep the generic chart for single-destination OTLP log backends such as Honeycomb and New Relic.

## [1.0.0] - 2026-06-02

- Add a parameterized OTLP/HTTP logs collector with Secret-backed authentication.
