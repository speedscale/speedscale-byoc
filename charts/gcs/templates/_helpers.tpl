{{- define "gcs.name" -}}
{{- printf "%s-gcs" .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- define "gcs.serviceAccount" -}}
{{- default (include "gcs.name" .) .Values.serviceAccount.name -}}
{{- end -}}
{{- define "gcs.readerName" -}}
{{- printf "%s-gcs-reader" .Release.Name | trunc 61 | trimSuffix "-" -}}
{{- end -}}
{{- define "gcs.readerServiceAccount" -}}
{{- default (include "gcs.readerName" .) .Values.reader.serviceAccount.name -}}
{{- end -}}
