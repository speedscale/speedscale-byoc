# Changelog

## [2.1.0] - 2026-06-19

### Added
- Optional workload-aware S3 prefixes under `namespace=<namespace>/app=<appLabel>/...`.
- `_speedscale/byoc-layout.json` manifest upload for proxymock import auto-detection.
- Configurable base S3 prefix via `s3.prefix`.

## [2.0.0] - 2026-06-18

### Changed
- Replaced the Fluent Bit S3 writer with the OTel Collector `awss3` exporter.
- Kept the `fluentbit-s3` chart path for compatibility with existing GitOps references.
- Changed new-install Secret and ServiceAccount defaults to `byoc-s3`.

## [1.0.0] - 2026-05-28

Initial release.

### Added
- OTel Collector + Fluent Bit pipeline shipping RRPair logs to Amazon S3
- Static IAM credentials (Secret) and EKS IRSA support
- Hive-partitioned key format for Athena/Glue compatibility
- Full Verify, Troubleshoot, Upgrade, and Configuration reference documentation
- Athena CREATE TABLE example in README
