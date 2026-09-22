{{- define "truehear-rabbitmq.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride -}}
{{- else -}}
{{- .Release.Name -}}
{{- end -}}
{{- end -}}

{{- define "truehear-rabbitmq.selectorLabels" -}}
app.kubernetes.io/name: rabbitmq
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
