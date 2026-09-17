# Splunk HEC security channel

This chart receives Speedscale OTLP logs, maps common network and HTTP fields into security-oriented attributes, and sends them to Splunk HEC. The default metadata-only profile removes request and response payloads after extracting those fields; set `securityProfile.includeFullPayload=true` only when the destination is approved for captured payload data.

Create the token Secret and install one release per destination:

```sh
kubectl -n byoc-splunk create secret generic byoc-splunk --from-literal=token='<HEC_TOKEN>'
helm upgrade --install byoc-splunk speedscale-byoc/splunk -n byoc-splunk --create-namespace --set splunk.endpoint=https://splunk.example.com:8088/services/collector
```

Point the Speedscale Forwarder at `byoc-splunk-collector.byoc-splunk.svc.cluster.local:4317`. Keep the NetworkPolicy enabled and list every namespace that is allowed to send OTLP.
