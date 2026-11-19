{{- define "iceberg.name" -}}
{{- printf "%s-iceberg" .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "iceberg.serviceAccount" -}}
{{- default (include "iceberg.name" .) .Values.serviceAccount.name -}}
{{- end -}}

{{- define "iceberg.image" -}}
{{- if .Values.image.digest -}}
{{- printf "%s@%s" .Values.image.repository .Values.image.digest -}}
{{- else -}}
{{- printf "%s:%s" .Values.image.repository .Values.image.tag -}}
{{- end -}}
{{- end -}}

{{/* Kubernetes memory quantity to MiB, as a float string. */}}
{{- define "iceberg.memoryMiB" -}}
{{- $q := toString . -}}
{{- $units := dict "Ki" 0.0009765625 "Mi" 1.0 "Gi" 1024.0 "Ti" 1048576.0 "k" 0.00095367431640625 "K" 0.00095367431640625 "M" 0.95367431640625 "G" 976.5625 "T" 953674.31640625 -}}
{{- $unit := regexFind "[A-Za-z]+$" $q -}}
{{- $number := float64 (trimSuffix $unit $q) -}}
{{- if eq $unit "" -}}
{{- divf $number 1048576.0 -}}
{{- else if hasKey $units $unit -}}
{{- mulf $number (get $units $unit) -}}
{{- else -}}
{{- fail (printf "unsupported memory quantity %q" $q) -}}
{{- end -}}
{{- end -}}

{{/* GOMEMLIMIT: explicit value, or 80% of the container memory limit. */}}
{{- define "iceberg.goMemLimit" -}}
{{- if .Values.goMemLimit -}}
{{- .Values.goMemLimit -}}
{{- else if and .Values.resources.limits .Values.resources.limits.memory -}}
{{- $mib := include "iceberg.memoryMiB" .Values.resources.limits.memory | float64 -}}
{{- printf "%dMiB" (int64 (floor (mulf $mib 0.8))) -}}
{{- end -}}
{{- end -}}

{{/* Catalog and credential environment shared by the sink and the compaction job. */}}
{{- define "iceberg.env" -}}
{{- with .Values.iceberg.catalog.propertiesSecret }}
{{- if .name }}
- name: ICEBERG_CATALOG_PROPERTIES
  valueFrom:
    secretKeyRef:
      name: {{ .name | quote }}
      key: {{ .key | quote }}
{{- end }}
{{- end }}
{{- if .Values.auth.googleCredentialsKey }}
- name: GOOGLE_APPLICATION_CREDENTIALS
  value: /var/run/iceberg/google/credentials.json
{{- end }}
{{- with .Values.extraEnv }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{- define "iceberg.envFrom" -}}
- configMapRef:
    name: {{ include "iceberg.name" . }}
{{- if eq .Values.auth.mode "secret" }}
- secretRef:
    name: {{ .Values.auth.secretName | quote }}
{{- end }}
{{- end -}}

{{- define "iceberg.podLabels" -}}
{{- if .Values.azureWorkloadIdentity }}
azure.workload.identity/use: "true"
{{- end }}
{{- with .Values.podLabels }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{- define "iceberg.credentialVolumeMounts" -}}
{{- if .Values.auth.googleCredentialsKey }}
- {name: google-credentials, mountPath: /var/run/iceberg/google, readOnly: true}
{{- end }}
{{- end -}}

{{- define "iceberg.credentialVolumes" -}}
{{- if .Values.auth.googleCredentialsKey }}
- name: google-credentials
  secret:
    secretName: {{ .Values.auth.secretName | quote }}
    items:
      - {key: {{ .Values.auth.googleCredentialsKey | quote }}, path: credentials.json}
{{- end }}
{{- end -}}

{{- define "iceberg.podSecurityContext" -}}
runAsNonRoot: true
runAsUser: 2100
runAsGroup: 0
seccompProfile:
  type: RuntimeDefault
{{- end -}}

{{- define "iceberg.containerSecurityContext" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities:
  drop: ["ALL"]
{{- end -}}
