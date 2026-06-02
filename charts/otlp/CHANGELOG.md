# Changelog

## [1.0.0] - 2026-06-02

First public release on Artifact Hub.

### Added
- Generic OTLP-export chart: one OTel Collector with an `otlphttp` exporter,
  configured per vendor by values (endpoint + auth header + compression).
- Per-vendor values presets in `examples/`: Dynatrace, Datadog, Honeycomb, New Relic.
- ArtifactHub annotations in Chart.yaml; values.schema.json validation.

### Notes
- **Supersedes the standalone `charts/dynatrace` chart** (closed before merge).
  All OTLP-native vendors share the identical Collector + `otlphttp` exporter —
  only the logs endpoint URL and the auth header differ — so a single
  parameterized chart with per-vendor presets replaces one-chart-per-vendor.
- The API token/key lives in a K8s Secret (data key `token`), created
  out-of-band; the chart references but does not manage it.
