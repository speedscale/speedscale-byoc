# Kafka security and data pipeline bridge

This chart receives Speedscale OTLP logs and publishes OTLP JSON records to Kafka for downstream consumers such as Splunk, Elastic, OpenSearch, Flink, and custom detection pipelines. The default security profile extracts useful network and HTTP fields and removes captured payloads. Set `securityProfile.includeFullPayload=true` only for an approved full-fidelity pipeline.

Install the chart, then point the Forwarder at `<release>-collector.<namespace>.svc.cluster.local:4317`. The default file-backed queue retries indefinitely across collector restarts, which is appropriate for an archive or event-bus handoff.
