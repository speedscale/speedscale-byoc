{{- define "gcs-datadog.name" -}}
{{- printf "%s-gcs-datadog" .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- define "gcs-datadog.serviceAccount" -}}
{{- default (include "gcs-datadog.name" .) .Values.serviceAccount.name -}}
{{- end -}}
