# Changelog

## [1.0.0] - 2026-05-28

First public release on Artifact Hub.

### Added
- Complete Verify, Troubleshoot, Upgrade, and Configuration reference sections in README
- ArtifactHub annotations in Chart.yaml
- EKS/IRSA alternative note in prerequisites
- `--dry-run` guidance in replay section

### Changed
- Chart name changed from `byoc-fluentbit` to `fluentbit` to match multi-chart repo convention
- Source URL updated to `github.com/speedscale/speedscale-byoc`

### Notes
- **Fluent Bit >= 4.0.3 required.** FB 3.x collapses OTLP `ResourceLogs` batches, discarding individual log records. The chart pins 4.0.3.

## [0.2.0] - 2026-04

- Switch output from HTTP bridge to native GCS via S3-compatible XML API
- Hive-partitioned key format for BigQuery external table compatibility
- Add `gcs-gather.py` for snapshot extraction from the archive

## [0.1.0] - 2026-03

- Initial chart: OTel Collector + Fluent Bit with HTTP output
