# Changelog

## [1.0.0] - 2026-06-02

First public release.

### Added
- Collector-only BYOC backend: OTel Collector → Dynatrace OTLP logs ingest.
- `otlphttp/dynatrace` exporter with gzip compression; the collector injects the
  `Api-Token` Authorization header the Forwarder can't add.
- Logs pipeline (`otlp` receiver → `batch` → `otlphttp/dynatrace` + `debug`).
- `values.schema.json`, ArtifactHub annotations, ArgoCD example, README with
  Verify and Troubleshoot sections.
