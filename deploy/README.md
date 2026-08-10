# Deploying the SAS Viya MCP Server on Kubernetes

Two equivalent artifacts, both serving the MCP endpoint at
`https://<viya-host>/mcp`:

| | Path | Use when |
|---|---|---|
| **Sample manifest** | `k8s/sas-mcp-server.yaml` | One environment, values edited in place. Read this first — it is the clearest statement of what gets deployed. |
| **Helm chart** | `helm/sas-mcp-server/` | Several environments, or the settings belong in a values file. |

Defaults in both target `viya4-s2.zeus.sashq-d.openstack.sas.com`, namespace
`llm`, reusing the existing `llm-tls-certs` secret and the `nginx` ingress
class — matching the pattern in the other deployments on that cluster.

Verified with `helm lint`, `helm template`, and `kubectl apply --dry-run=client`
against a v1.35 client. **Not yet applied to a live cluster.**

---

## The one thing worth understanding first

The MCP endpoint is at `/mcp`, but **the OAuth endpoints are its siblings at the
host root, not its children.** Enumerating the running app's routes gives:

```
/mcp                                         <- the MCP endpoint
/authorize  /token  /register  /consent      <- OAuth 2.0, at the ROOT
/auth/callback
/.well-known/oauth-authorization-server
/.well-known/oauth-protected-resource/mcp
```

An MCP client discovers those URLs automatically: it calls `/mcp`, gets a 401
carrying `resource_metadata="https://<host>/.well-known/oauth-protected-resource/mcp"`,
and follows the chain from there. Confirmed by driving the real app:

```
POST /mcp -> 401
  WWW-Authenticate: Bearer …, resource_metadata="https://viya4-s2…/.well-known/oauth-protected-resource/mcp"

GET /.well-known/oauth-authorization-server -> 200
  authorization_endpoint: https://viya4-s2…/authorize
  token_endpoint:         https://viya4-s2…/token
  registration_endpoint:  https://viya4-s2…/register
```

So routing only `/mcp` gets you a server that a client can reach and never sign
in to. **All eight paths must reach the service**, which is why the ingress
lists them explicitly.

Those paths are claimed on a host Viya already owns. Viya's own OAuth lives
under `/SASLogon/oauth/*`, so they should be free — but check before applying:

```sh
kubectl get ingress -A -o jsonpath='{range .items[*]}{.metadata.namespace}{"\t"}{.metadata.name}{"\t"}{.spec.rules[*].http.paths[*].path}{"\n"}{end}'
```

If a collision shows up, see [Alternative: everything under `/mcp`](#alternative-everything-under-mcp).

---

## Prerequisites

**1. Signing key.** The server derives its JWT signing key from
`MCP_SIGNING_KEY`. Unset, it falls back to the literal string `"default"` —
published in this open-source repo, so anyone can derive the key and forge
tokens. Create a real one:

```sh
kubectl -n llm create secret generic sas-mcp-server \
  --from-literal=signing-key="$(openssl rand -base64 32)"
```

The derivation is deterministic, so replicas sharing the secret issue
compatible tokens. Your local `.env` already has a good value if you would
rather keep the two environments aligned — but pass it through the secret, not
through a values file.

**2. SASLogon redirect URI.** Register this on OAuth client `sas-mcp-gerdaw`:

```
https://viya4-s2.zeus.sashq-d.openstack.sas.com/auth/callback
```

Without it the browser sign-in dead-ends after the Viya login page. This is a
Viya-side change; the deployment cannot do it for you.

**3. Image pull.** The manifests use `ghcr.io/sassoftware/sas-mcp-server:1.8.0`.
If the cluster cannot pull from ghcr.io, mirror it first:

```sh
podman pull ghcr.io/sassoftware/sas-mcp-server:1.8.0
podman tag ghcr.io/sassoftware/sas-mcp-server:1.8.0 \
  registry.zeus.sashq-d.openstack.sas.com/library/sas-mcp-server:1.8.0
podman push registry.zeus.sashq-d.openstack.sas.com/library/sas-mcp-server:1.8.0
```

then set `image.repository` accordingly.

---

## Deploy

### Sample manifest

```sh
kubectl apply -f deploy/k8s/sas-mcp-server.yaml
kubectl -n llm rollout status deploy/sas-mcp-server
```

### Helm

```sh
helm upgrade --install sas-mcp deploy/helm/sas-mcp-server \
  --namespace llm \
  --set fullnameOverride=sas-mcp-server
```

Override per environment:

```sh
helm upgrade --install sas-mcp deploy/helm/sas-mcp-server -n llm \
  --set viya.endpoint=https://other-viya.example.com \
  --set ingress.host=other-viya.example.com \
  --set viya.clientId=sas-mcp-prod \
  --set server.tiers=0-4 \
  --set persistence.type=pvc
```

`MCP_BASE_URL` is derived from `ingress.host` and `ingress.tls.enabled`, so it
cannot drift out of step with the ingress.

### Verify

```sh
kubectl -n llm port-forward svc/sas-mcp-server 8134:8134
curl -s localhost:8134/health
# {"status":"healthy","service":"sas-viya-execution-mcp"}

# through the ingress — a 401 with a resource_metadata pointer is CORRECT,
# it is how a client discovers the sign-in flow
curl -isk https://viya4-s2.zeus.sashq-d.openstack.sas.com/mcp -X POST \
  -H 'content-type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"initialize"}' | head -20
```

Then point an MCP client at `https://viya4-s2.zeus.sashq-d.openstack.sas.com/mcp`.

### Both auth paths work here

`ALLOW_RAW_BEARER` is additive, not a replacement — HTTP mode always runs PKCE,
so the browser sign-in and token-bearing clients share the one `/mcp` endpoint.
Verified against the deployment: dynamic client registration returns a
`client_id`, and `/authorize` 302s onward into the consent step.

**This topology also sidesteps the CSP problem** that `examples/configuration.md`
warns about. Viya sends `Content-Security-Policy: form-action 'self'`, which
blocks the post-login redirect to an MCP server on a *different* origin — the
reason local development is told to disable the directive. Because this
deployment serves `/auth/callback` on the **Viya host itself**, the callback is
same-origin and `'self'` permits it. Nothing needs disabling.

That advantage disappears if you move the server to its own hostname.

---

## Deliberate choices

**`replicas: 1`.** Two things break at more than one replica, neither fixable in
a manifest:

- OAuth client registrations live in a per-pod file-tree store, so a client
  registered on pod A is unknown to pod B. `OAuthProxy` accepts a
  `client_storage=` argument for a shared backend, but this repo does not wire
  it yet.
- Warm Viya compute sessions are cached in process, so the same user hitting a
  different pod spins up a second session on Viya, and `reset_compute_session`
  only resets whichever pod serves that call.

The chart warns if you raise it anyway.

**`readOnlyRootFilesystem: true` plus a state volume.** The OAuth store needs a
writable path, and it is needed *at import*: `OAuthProxy.__init__` mkdirs its
storage directory, and `config.py` constructs the proxy at module scope. Without
the volume the container **crash-loops at startup** with
`OSError: [Errno 30] Read-only file system` — it does not start healthy and fail
later. The chart fails the render on that combination rather than let you find
out live. With `emptyDir` (the default) registrations are lost on every restart
and clients re-register; `persistence.type=pvc` keeps them.

**`terminationGracePeriodSeconds: 60`.** Shutdown issues one `DELETE` per warm
compute session. Measured 2.4 s with none cached; with many warm users this
scales with Viya latency, and the 30 s default would leak sessions on rollout.

**nginx timeout and buffering annotations.** MCP streamable HTTP holds
long-lived connections. nginx's 60 s default read timeout would cut a long SAS
execution mid-call, and default buffering would stall streamed output until the
response completed.

**No node affinity or tolerations.** The other workloads on this cluster pin to
the `app=llm` node pool because they are GPU model servers. This is a light
Python service with no such need, so it schedules anywhere. Set
`nodeSelector`/`tolerations` if you want it on that pool regardless.

---

## Usage telemetry (optional, off by default)

The server can log every tool call to a JSONL file — tool name, arguments, the
model's stated `goal`, status, error, and latency. The chart wires it up; it is
disabled unless you ask for it:

```sh
helm upgrade sas-mcp deploy/helm/sas-mcp-server -n llm --reuse-values \
  --set telemetry.enabled=true \
  --set telemetry.persistence.type=pvc      # else the log dies with the pod
```

That sets `COLLECTION_MODE` and friends, and mounts a volume at the log's
directory. Read it back with:

```sh
kubectl -n llm exec deploy/sas-mcp-server -- cat /var/log/sas-mcp/tool-usage.log
```

**Before enabling this on a shared server**, be clear about what it is: the log
captures the SAS code and queries submitted by *every user of the deployment*,
not just you, along with the model's stated reason for each call. Redaction
covers credential-shaped keys, inline Bearer/JWT tokens and the Viya hostname —
it does **not** detect PII in data values. On a laptop this is self-consent; on
a shared deployment it is collecting other people's work, so tell them first.
Nothing is transmitted anywhere: the log stays on the volume until someone
deliberately copies it off.

**One version trap.** `telemetry.logResults` is a tri-state (`never` /
`failures` / `always`) as of the **schema-v3** work, which is merged to `main`
(#40) but **not yet in a released image** — `image.tag` still defaults to
`1.8.0`. The 1.8.0 image parses the same variable with `env_bool`, which accepts
only `true/1/yes/on` and `false/0/no/off` and silently falls back to its default
for anything else, so `failures` there quietly means shape-only. `"true"` and
`"false"` mean the same thing on both builds, so the chart defaults to `"false"`
and the install notes warn if you set a v3-only value.

Once a release ships a v3 image and `image.tag` is bumped, `failures` becomes
the useful setting — full result bodies only for calls that errored or whose
tool declared a failure — and the warning stops applying.

---

## Alternative: everything under `/mcp`

If claiming root paths on the Viya host is unacceptable, the server can instead
serve *everything* under `/mcp`, leaving only one root path in play. This needs
a small code change — `mcp_server.py` currently hardcodes `mcp.http_app()`:

```python
app = mcp.http_app(path="/")            # mount at the root of the container
```

with `MCP_BASE_URL=https://<host>/mcp` and an ingress that strips the prefix
(`nginx.ingress.kubernetes.io/rewrite-target: /$2`, path `/mcp(/|$)(.*)`, the
pattern the other deployments on this cluster already use).

Tested: the OAuth endpoints then advertise as `https://<host>/mcp/authorize`,
`/mcp/token`, `/mcp/register`. **One root path still remains** —
`/.well-known/oauth-protected-resource/mcp/` — because RFC 9728 places
protected-resource metadata at the host root by design, and FastMCP follows it.

So this trades seven root paths for one, at the cost of a code change. Worth
doing if the collision check turns up trouble; not worth doing pre-emptively.

A third option avoids the OAuth paths entirely: set `ingress.oauthEnabled=false`
and have clients pass a Viya token directly (`ALLOW_RAW_BEARER=true`, already on
by default here). That collapses the ingress to the single `/mcp` rule, but the
browser sign-in flow is then unavailable — every client must bring its own
token.

---

## Not covered

- No live-cluster apply. Schema-valid and rendering correctly is not the same as
  running.
- `SSL_VERIFY=false` is set for the self-signed Viya certificate. See
  `FASTMCP-4-IMPACT.md` for why that flag needs revisiting before a FastMCP 4
  upgrade — the patch it relies on does not cover FastMCP 4's HTTP client.
- No live-cluster load or failover testing beyond what `SCALING.md` records.
- No NetworkPolicy, PodDisruptionBudget, or HPA — an HPA in particular would be
  actively wrong while replicas are capped at 1.
