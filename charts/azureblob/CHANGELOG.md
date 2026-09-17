# Changelog

## [1.1.0] - 2026-09-17

- Upgrade the collector to digest-pinned 0.160.0.
- Add a persistent queue with indefinite archive retries, resource controls, rollout checksums, pod hardening, and restricted OTLP ingress.

## [1.0.0] - 2026-06-02

First public release on Artifact Hub.

### Added
- OTel Collector pipeline shipping RRPair logs to Azure Blob Storage via the
  collector-native `azureblob` exporter (OTLP-JSON blobs, no Fluent Bit).
- Connection-string authentication via an out-of-band K8s Secret.
- Verify, Troubleshoot, Upgrade, and Configuration reference sections in README.
- ArtifactHub annotations in Chart.yaml.

### Notes
- **Pins `otel/opentelemetry-collector-contrib:0.153.0` — newer than the sibling
  charts (0.108.0).** The `azureblob` exporter is alpha and did not ship in the
  otelcol-contrib image until ~v0.153.0, so the 0.108.0 image used by the
  GCS/S3 charts does not contain it (and 0.123.0 fails with
  `unknown type: azureblob`). 0.153.0 is a released tag that includes the
  exporter.
