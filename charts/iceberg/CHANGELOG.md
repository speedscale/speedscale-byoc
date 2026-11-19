# Changelog

## [1.0.0] - 2026-11-19

- Add the Iceberg channel: the Speedscale `iceberg-sink` receives RRPairs over OTLP/gRPC and appends them to an Apache Iceberg table through AWS S3 Tables, AWS Glue, or a REST catalog.
- Configure every sink setting from values, with catalog properties from a Secret, workload identity or Secret-backed storage credentials, and `GOMEMLIMIT` derived from the memory limit.
- Add an optional compaction CronJob for self-managed REST catalogs that runs the sink's `compact` mode by default, a restricted NetworkPolicy, an optional ServiceMonitor, and a pre-stop delay so rolling restarts do not drop Forwarder exports.
