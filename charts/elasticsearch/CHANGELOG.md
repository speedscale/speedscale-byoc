# Changelog

## [1.0.0] - 2026-05-28

First public release on Artifact Hub.

### Added
- Complete Verify, Troubleshoot, Upgrade, and Configuration reference sections in README
- ArtifactHub annotations in Chart.yaml
- `examples/operator-values.yaml` with pre-wired forwarder config
- Note on `Resource.cluster` vs `Body.cluster` workaround

### Changed
- Chart name changed from `byoc-elasticsearch` to `elasticsearch` to match multi-chart repo convention
- Source URL updated to `github.com/speedscale/speedscale-byoc`

## [0.1.0] - 2026-04

- Initial chart: Elasticsearch 8.14.3, Kibana 8.14.3, OTel Collector 0.108.0
- NodePorts: ES 30032, Kibana 30033
- Auto-provisions Kibana dashboard via ndjson import
