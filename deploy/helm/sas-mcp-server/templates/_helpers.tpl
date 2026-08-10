{{/* Chart name, overridable. */}}
{{- define "sas-mcp-server.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Fully qualified app name. */}}
{{- define "sas-mcp-server.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "sas-mcp-server.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "sas-mcp-server.labels" -}}
helm.sh/chart: {{ include "sas-mcp-server.chart" . }}
{{ include "sas-mcp-server.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "sas-mcp-server.selectorLabels" -}}
app.kubernetes.io/name: {{ include "sas-mcp-server.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "sas-mcp-server.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "sas-mcp-server.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/*
The externally reachable base URL. The server advertises its OAuth endpoints
relative to this, so a wrong value sends clients somewhere that does not exist.
Deliberately the host ROOT with no path: the app mounts the MCP endpoint at
.Values.ingress.mcpPath itself, and appending the path here would make the
advertised resource URL doubled (…/mcp/mcp).
*/}}
{{- define "sas-mcp-server.baseUrl" -}}
{{- if .Values.ingress.enabled -}}
{{- $scheme := ternary "https" "http" .Values.ingress.tls.enabled -}}
{{- printf "%s://%s" $scheme .Values.ingress.host -}}
{{- else -}}
{{- printf "http://localhost:%d" (int .Values.server.port) -}}
{{- end -}}
{{- end -}}

{{/* Secret holding the JWT signing key. */}}
{{- define "sas-mcp-server.signingSecretName" -}}
{{- if .Values.signingKey.value -}}
{{- printf "%s-signing" (include "sas-mcp-server.fullname" .) -}}
{{- else -}}
{{- required "signingKey.existingSecret is required when signingKey.value is empty — see values.yaml" .Values.signingKey.existingSecret -}}
{{- end -}}
{{- end -}}

{{- define "sas-mcp-server.signingSecretKey" -}}
{{- if .Values.signingKey.value -}}signing-key{{- else -}}{{ .Values.signingKey.existingSecretKey }}{{- end -}}
{{- end -}}
