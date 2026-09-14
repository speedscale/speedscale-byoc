{{- define "gcs-datadog.name" -}}
{{- printf "%s-gcs-datadog" .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- define "gcs-datadog.serviceAccount" -}}
{{- default (include "gcs-datadog.name" .) .Values.serviceAccount.name -}}
{{- end -}}
{{- define "gcs-datadog.readerName" -}}
{{- printf "%s-gcs-reader" .Release.Name | trunc 61 | trimSuffix "-" -}}
{{- end -}}
{{- define "gcs-datadog.readerServiceAccount" -}}
{{- default (include "gcs-datadog.readerName" .) .Values.reader.serviceAccount.name -}}
{{- end -}}
