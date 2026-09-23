{{- define "truehear-redis.fullname" -}}
{{- $name := default .Release.Name .Values.fullnameOverride -}}
{{- if not (regexMatch "^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$" $name) -}}
{{- fail "Redis release name must be a lowercase Kubernetes DNS label" -}}
{{- end -}}
{{- $name -}}
{{- end -}}

{{- define "truehear-redis.selectorLabels" -}}
app.kubernetes.io/name: redis
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
