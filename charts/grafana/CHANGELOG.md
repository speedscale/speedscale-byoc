# Changelog

## [1.0.0] - 2026-05-28

First public release on Artifact Hub.

### Added
- Complete Verify, Troubleshoot, Upgrade, and Configuration reference sections in README
- ArtifactHub annotations in Chart.yaml
- `examples/operator-values.yaml` with pre-wired forwarder config

### Changed
- Chart name changed from `byoc-grafana` to `grafana` to match multi-chart repo convention (`helm install ... speedscale-byoc/grafana`)
- Source URL updated to `github.com/speedscale/speedscale-byoc`

## [0.1.1] - 2026-04

- Pin NodePorts: Grafana 30030, Loki 30031
- Add Prometheus scraping for forwarder + nettap metrics
- Add Speedscale BYOC and Speedscale Traffic Grafana dashboards (auto-provisioned)

## [0.1.0] - 2026-03

- Initial chart: Loki 2.9.8, Grafana 11.1.4, OTel Collector 0.108.0
