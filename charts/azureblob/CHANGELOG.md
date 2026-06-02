# Changelog

## [1.0.0] - 2026-06-02

First public release on Artifact Hub.

### Added
- OTel Collector pipeline shipping RRPair logs to Azure Blob Storage via the
  collector-native `azureblob` exporter (OTLP-JSON blobs, no Fluent Bit).
- Connection-string authentication via an out-of-band K8s Secret.
- Verify, Troubleshoot, Upgrade, and Configuration reference sections in README.
- ArtifactHub annotations in Chart.yaml.

### Notes
- **Pins `otel/opentelemetry-collector-contrib:0.123.0` — newer than the sibling
  charts (0.108.0).** The `azureblob` exporter is alpha and was added to
  opentelemetry-collector-contrib in v0.121.0, so the 0.108.0 image used by the
  GCS/S3 charts does not contain it. 0.123.0 is a released tag that includes the
  exporter.
