# Changelog

## [Unreleased]

### Added
- OTel Collector now derives traffic metrics from the RRPair log stream using `count` + `sum` connectors and remote-writes them to the bundled Prometheus — no forwarder change, nothing sent twice.
  - `speedscale_calls_total{svc,status}` — request count by service + HTTP status
  - `speedscale_request_duration_ms_sum_total{svc}` — summed request duration by service
- Dashboard aggregate panels (Requests, Error rate, Status distribution, Request rate by service, Avg latency, Avg latency by service) now read PromQL from Prometheus for stable, bounded performance as endpoint count and time windows grow.

### Changed
- Loki continues to back endpoint-level panels (Distinct endpoints, Top endpoints, Recent traffic) and replay pull-out — no change to that path.
- Latency panels currently show average latency; p50/p95/p99 percentiles are a planned follow-up requiring span-level histograms (`spanmetrics` connector).

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
