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
The path prefix the server is mounted under, normalised: "" or "/something"
with no trailing slash. Everything else derives from this, so normalise once.
*/}}
{{- define "sas-mcp-server.pathPrefix" -}}
{{- $p := .Values.ingress.pathPrefix | default "" | trimSuffix "/" -}}
{{- if and $p (not (hasPrefix "/" $p)) -}}{{- $p = printf "/%s" $p -}}{{- end -}}
{{- $p -}}
{{- end -}}

{{/*
RFC 9728 resource metadata path. The app derives this from MCP_BASE_URL's path,
so it CARRIES the prefix and is served at the host root with the prefix inside
it: /.well-known/oauth-protected-resource/sas-mcp/mcp. Forward it unrewritten.
*/}}
{{- define "sas-mcp-server.resourceMetadataPath" -}}
{{- printf "/.well-known/oauth-protected-resource%s%s" (include "sas-mcp-server.pathPrefix" .) .Values.ingress.mcpPath -}}
{{- end -}}

{{/*
RFC 8414 §3 authorization-server metadata path AS THE CLIENT ASKS FOR IT: the
issuer's path is appended to the well-known segment. The app serves the
document at the bare /.well-known/oauth-authorization-server, so this one has
to be rewritten back to that.
*/}}
{{- define "sas-mcp-server.asMetadataPath" -}}
{{- printf "/.well-known/oauth-authorization-server%s" (include "sas-mcp-server.pathPrefix" .) -}}
{{- end -}}

{{/*
The externally reachable base URL, INCLUDING any path prefix. The server
advertises its OAuth endpoints relative to this, so a wrong value sends clients
somewhere that does not exist. It stops at the prefix and never includes
.Values.ingress.mcpPath: the app mounts the MCP endpoint at that path itself,
and appending it here would double it (…/mcp/mcp).
*/}}
{{- define "sas-mcp-server.baseUrl" -}}
{{- if .Values.ingress.baseUrlOverride -}}
{{- .Values.ingress.baseUrlOverride | trimSuffix "/" -}}
{{- else if .Values.ingress.enabled -}}
{{- /* `if`, not sprig `ternary`: ternary demands a literal bool and aborts the
       whole render on a quoted "true" or a --set-string override. */ -}}
{{- $scheme := "http" -}}
{{- if .Values.ingress.tls.enabled -}}{{- $scheme = "https" -}}{{- end -}}
{{- printf "%s://%s%s" $scheme .Values.ingress.host (include "sas-mcp-server.pathPrefix" .) -}}
{{- else -}}
{{- /* NOT localhost: that resolves to the CLIENT, so every advertised OAuth
       endpoint would point at the caller's own machine and sign-in could never
       complete. The in-cluster Service URL is at least reachable by the
       in-cluster callers this mode is for; set ingress.baseUrlOverride when
       something in front of the Service terminates the real URL. */ -}}
{{- printf "http://%s.%s.svc.cluster.local:%d" (include "sas-mcp-server.fullname" .) .Release.Namespace (int .Values.server.port) -}}
{{- end -}}
{{- end -}}

{{/*
Guards for configurations that render valid YAML but cannot work. Failing here
beats a green install that 404s, crash-loops, or is rejected at apply time.
*/}}
{{- define "sas-mcp-server.validate" -}}
{{- if ne .Values.ingress.mcpPath "/mcp" -}}
{{- fail (printf "ingress.mcpPath is %q but the application hardcodes /mcp (mcp_server.py does mcp.http_app() with no path=). Routing anywhere else returns 404. Leave it as /mcp." .Values.ingress.mcpPath) -}}
{{- end -}}
{{- if .Values.ingress.enabled -}}
{{- if not (has .Values.ingress.controller (list "contour" "nginx")) -}}
{{- fail (printf "ingress.controller is %q; it must be \"contour\" or \"nginx\"." .Values.ingress.controller) -}}
{{- end -}}
{{- if and .Values.ingress.className (ne .Values.ingress.controller "nginx") -}}
{{- fail (printf "ingress.className is set to %q but ingress.controller is %q. Chart 0.2.0 made Contour the default renderer, so an unchanged 0.1.x values file would silently stop rendering your Ingress. Set ingress.controller=nginx to keep the previous behaviour, or clear ingress.className to go with Contour." .Values.ingress.className .Values.ingress.controller) -}}
{{- end -}}
{{- $prefix := include "sas-mcp-server.pathPrefix" . -}}
{{- if $prefix -}}
{{- if ne .Values.ingress.controller "contour" -}}
{{- fail (printf "ingress.pathPrefix (%q) is implemented for Contour only — it needs a replacePrefix rewrite plus two extra discovery routes, and the equivalent nginx rewrite is untested here. Use controller=contour, or give the server its own hostname and leave pathPrefix empty." .Values.ingress.pathPrefix) -}}
{{- end -}}
{{- if or (hasPrefix "/mcp" $prefix) (hasPrefix "/.well-known" $prefix) (hasPrefix "/SASLogon" $prefix) -}}
{{- fail (printf "ingress.pathPrefix %q collides with a path the app or Viya already serves at the host root. Pick something else, e.g. /sas-mcp." $prefix) -}}
{{- end -}}
{{- end -}}
{{- if eq .Values.ingress.controller "contour" -}}
{{- if and .Values.ingress.contour.rootProxy.name (not .Values.ingress.contour.rootProxy.namespace) -}}
{{- fail "ingress.contour.rootProxy.name is set but .namespace is empty. A child HTTPProxy nobody includes is an orphan: it renders, it applies, and it routes nothing. Set the namespace of the root proxy (the Viya namespace), or clear .name to render a standalone root proxy that owns the FQDN itself." -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- if and .Values.securityContext.readOnlyRootFilesystem (not .Values.persistence.enabled) -}}
{{- fail "persistence.enabled=false with securityContext.readOnlyRootFilesystem=true cannot start: OAuthProxy.__init__ mkdirs its store at import time, so the container crash-loops on a read-only filesystem. Enable persistence or set readOnlyRootFilesystem=false." -}}
{{- end -}}
{{- if .Values.telemetry.enabled -}}
{{- $logDir := dir .Values.telemetry.logPath -}}
{{- if eq $logDir "/tmp" -}}
{{- fail "telemetry.logPath must not sit directly under /tmp — it would render two volumeMounts on /tmp and the API server rejects duplicate mountPaths. Use e.g. /var/log/sas-mcp/tool-usage.log." -}}
{{- end -}}
{{- if and .Values.persistence.enabled (eq $logDir .Values.persistence.mountPath) -}}
{{- fail "telemetry.logPath's directory collides with persistence.mountPath — duplicate mountPath is rejected at apply time." -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/* True when any ReadWriteOnce PVC is attached, so the Deployment must not
     RollingUpdate into a Multi-Attach deadlock. */}}
{{- define "sas-mcp-server.hasRWO" -}}
{{- if or (and .Values.persistence.enabled (eq .Values.persistence.type "pvc")) (and .Values.telemetry.enabled (eq .Values.telemetry.persistence.type "pvc")) -}}true{{- end -}}
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
