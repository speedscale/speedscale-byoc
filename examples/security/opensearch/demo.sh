#!/bin/sh
set -eu

cd "$(dirname "$0")"

admin_password=Speedscale-OpenSearch-Demo-2026!
opensearch_url=https://localhost:9200

docker compose up --detach

printf 'Waiting for OpenSearch'
until curl --silent --fail --insecure --user "admin:${admin_password}" "${opensearch_url}/_cluster/health" >/dev/null 2>&1; do
  printf '.'
  sleep 3
done
printf '\n'

printf 'Waiting for OTLP ingestion'
until sed "s/TIME_UNIX_NANO/$(date +%s)000000000/g" sample-auth-failure.json | curl --silent --show-error --fail --request POST http://localhost:4318/v1/logs -H 'Content-Type: application/json' --data-binary @- >/dev/null 2>&1; do
  printf '.'
  sleep 3
done
printf '\n'

printf 'Waiting for the normalized event'
event_count=0
attempts=0
while [ "$event_count" -lt 1 ] && [ "$attempts" -lt 60 ]; do
  sleep 3
  event_count=$(curl --silent --insecure --user "admin:${admin_password}" "${opensearch_url}/speedscale-security-*/_count" -H 'Content-Type: application/json' -d '{"query":{"term":{"speedscale.traffic_id.keyword":"security-demo-auth-failure"}}}' | python3 -c 'import json,sys; print(json.load(sys.stdin).get("count", 0))')
  attempts=$((attempts + 1))
  printf '.'
done
printf '\n'
if [ "$event_count" -lt 1 ]; then
  printf 'The sample event did not reach OpenSearch within three minutes. Check docker compose logs.\n' >&2
  exit 1
fi

document=$(curl --silent --fail --insecure --user "admin:${admin_password}" "${opensearch_url}/speedscale-security-*/_search" -H 'Content-Type: application/json' -d '{"size":1,"query":{"term":{"speedscale.traffic_id.keyword":"security-demo-auth-failure"}}}')
printf '%s' "$document" | python3 -c 'import json,sys; source=json.load(sys.stdin)["hits"]["hits"][0]["_source"]; required={("event","kind"):"event",("http","response","status_code"):401,("url","path"):"/api/login",("client","address"):"203.0.113.42"}; value=lambda path: __import__("functools").reduce(lambda item,key:item.get(key,{}) if isinstance(item,dict) else {},path,source); missing={".".join(path):expected for path,expected in required.items() if value(path) != expected}; assert not missing, f"normalized fields missing: {missing}"; assert "must-not-leave-collector" not in json.dumps(source), "captured payload reached OpenSearch"; print("Verified normalized fields and metadata-only export.")'

rule_response=$(curl --silent --insecure --user "admin:${admin_password}" --request POST "${opensearch_url}/_plugins/_security_analytics/rules?category=network" -H 'Content-Type: application/json' --data-binary @rules/authentication-failure.yml)
rule_id=$(printf '%s' "$rule_response" | python3 -c 'import json,sys; response=json.load(sys.stdin); print(response.get("_id", ""))')
if [ -z "$rule_id" ]; then
  printf 'Unable to create the Sigma rule: %s\n' "$rule_response" >&2
  printf 'Reset this local demo with: docker compose down --volumes\n' >&2
  exit 1
fi

security_index=$(curl --silent --fail --insecure --user "admin:${admin_password}" "${opensearch_url}/_cat/indices/speedscale-security-*?h=index" | grep -E '^speedscale-security-[0-9]{4}-[0-9]{2}-[0-9]{2}$' | sort | tail -1)
if [ -z "$security_index" ]; then
  printf 'No detector-safe Speedscale security index was found.\n' >&2
  exit 1
fi
detector_body=$(python3 -c 'import json,sys; rule_id,index=sys.argv[1:]; print(json.dumps({"type":"detector","name":"Speedscale authentication detector","detector_type":"network","enabled":True,"schedule":{"period":{"interval":1,"unit":"MINUTES"}},"inputs":[{"detector_input":{"description":"Authentication failures observed in Speedscale traffic","indices":[index],"custom_rules":[{"id":rule_id}],"pre_packaged_rules":[]}}],"triggers":[]}))' "$rule_id" "$security_index")
detector_response=$(curl --silent --insecure --user "admin:${admin_password}" --request POST "${opensearch_url}/_plugins/_security_analytics/detectors" -H 'Content-Type: application/json' -d "$detector_body")
detector_id=$(printf '%s' "$detector_response" | python3 -c 'import json,sys; response=json.load(sys.stdin); print(response.get("_id", ""))')
if [ -z "$detector_id" ]; then
  printf 'Unable to create the detector: %s\n' "$detector_response" >&2
  printf 'Reset this local demo with: docker compose down --volumes\n' >&2
  exit 1
fi

sed "s/TIME_UNIX_NANO/$(date +%s)000000000/g" sample-auth-failure.json | curl --silent --show-error --fail --request POST http://localhost:4318/v1/logs -H 'Content-Type: application/json' --data-binary @- >/dev/null

printf 'Created detector %s. Open http://localhost:5601 and check Security Analytics > Findings after one minute.\n' "$detector_id"
