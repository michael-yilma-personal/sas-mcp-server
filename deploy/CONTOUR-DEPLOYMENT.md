# Deploying behind Contour

Contour is the default router the Helm chart renders (`ingress.controller: contour`). It is also the
only mode that can mount the server **under a path prefix on an existing hostname**, which is usually
the fastest way to get an MCP endpoint approved in an enterprise: no new certificate, no new DNS
record, no new load balancer.

> The path-prefix pattern documented here was contributed by GitHub user **sasaom-sdksbe**, from a
> working SAS Viya 4 + AKS + Contour deployment. `deploy/AKS-CONTOUR.md` covers what is specific to
> Azure.

---

## 1. Pick a shape

Two independent choices. The chart supports all four combinations; the interesting one is the
bottom-right.

|                         | **Host root** (`https://host/mcp`)                    | **Path prefix** (`https://host/sas-mcp/mcp`)               |
| ----------------------- | ----------------------------------------------------- | ---------------------------------------------------------- |
| **Standalone** proxy    | The MCP server owns the hostname and its certificate.  | Rare — you own the host but still want a prefix.             |
| **Delegated** child     | Viya's root proxy delegates `/mcp` and the OAuth paths.| Viya's root proxy delegates one prefix plus discovery paths. |

*Standalone* means the chart renders a root `HTTPProxy` with a `virtualhost`, so it claims the FQDN
and terminates TLS with `ingress.tls.secretName`. Two root proxies for one FQDN is a conflict Contour
resolves against you — do not do this on a host SAS Viya already owns.

*Delegated* means the chart renders **child** proxies with no `virtualhost`, and an existing root
proxy — SAS Viya deploys one, conventionally `sas-httpproxy-root` — includes them. The children
inherit the root's FQDN and certificate. Helm cannot patch a resource it does not own, so the chart
prints the exact `kubectl patch` in its NOTES; run it once.

---

## 2. The routing problem, stated exactly

The server serves everything at **its own root**, and advertises its OAuth endpoints relative to
`MCP_BASE_URL`. Those two facts are all you need, but they do not compose the way people expect once
a path prefix is involved. Enumerated from the running app's route table with
`MCP_BASE_URL=https://host/sas-mcp`:

| What the pod serves                                 | What the client requests                                  | Contour must  |
| --------------------------------------------------- | --------------------------------------------------------- | ------------- |
| `/mcp`                                              | `/sas-mcp/mcp`                                             | strip prefix  |
| `/authorize` `/token` `/register`                   | `/sas-mcp/authorize` … (as advertised)                     | strip prefix  |
| `/consent` `/auth/callback`                         | `/sas-mcp/consent`, `/sas-mcp/auth/callback`               | strip prefix  |
| `/.well-known/oauth-protected-resource/sas-mcp/mcp` | the same — RFC 9728, prefix lives **inside** the path      | forward as-is |
| `/.well-known/oauth-authorization-server`           | `/.well-known/oauth-authorization-server/sas-mcp` (RFC 8414 §3) | rewrite  |
| `/.well-known/oauth-authorization-server`           | the same, bare — clients that ignore the issuer path       | forward as-is |
| `/authorize` `/token` `/register`                   | the same, at the host root — same clients                  | forward as-is |

The two surprises:

- **The `.well-known` documents never move under the prefix.** RFC 9728 inserts the resource's path
  *into* the well-known path (`/.well-known/oauth-protected-resource` + `/sas-mcp` + `/mcp`), and RFC
  8414 §3 does the same for the issuer. Both stay at the host root, so both need routes of their own
  — and the app only serves the 8414 one at the bare path, so that one needs a rewrite.
- **Not every client honours the issuer's path.** A conformant client takes
  `authorization_endpoint` verbatim from the metadata document, which the server publishes with the
  prefix. VS Code (and Claude, in some versions) instead builds `new URL("/authorize", issuer)`,
  which discards the path and lands on the host root. The extra root-level routes exist for those
  clients — `ingress.contour.rootRelativeOAuth`, on by default. They claim paths on the shared Viya
  host, so if every client you support is conformant, turn them off.

Check what the server advertises for your own configuration rather than trusting this table — it
prints its own answer:

```sh
curl -s https://host/.well-known/oauth-protected-resource/sas-mcp/mcp | jq
curl -s https://host/.well-known/oauth-authorization-server | jq
```

---

## 3. Install (delegated, path prefix)

`my-values.yaml`:

```yaml
viya:
  endpoint: https://lab401.example.com
  clientId: sas-mcp
signingKey:
  existingSecret: sas-mcp-signing   # created out of band, see below
ingress:
  controller: contour               # the default; shown for clarity
  host: lab401.example.com          # the Viya hostname, whose certificate you are reusing
  pathPrefix: /sas-mcp
  contour:
    rootProxy:
      name: sas-httpproxy-root
      namespace: lab401             # the Viya namespace, not this one
```

```sh
kubectl create namespace sas-mcp
kubectl -n sas-mcp create secret generic sas-mcp-signing \
  --from-literal=signing-key="$(openssl rand -base64 32)"

helm -n sas-mcp install sas-mcp deploy/helm/sas-mcp-server -f my-values.yaml
```

The chart renders three children — the prefix route, a pass-through, and the RFC 8414 rewrite — and
prints the `kubectl patch` that delegates them. Run it, then:

```sh
kubectl -n sas-mcp get httpproxy                     # all "valid", none "orphaned"
kubectl -n lab401 get httpproxy sas-httpproxy-root   # still "valid"
```

Finally register the redirect URI on the SASLogon client — **with the prefix**, because the server
advertises its callback relative to `MCP_BASE_URL`:

```
https://lab401.example.com/sas-mcp/auth/callback
```

For the host-root shape, drop `pathPrefix`: the chart then delegates `/mcp` and the seven OAuth paths
directly, with no rewriting anywhere.

---

## 4. Verify

```sh
# 1. The landing page — proves routing and TLS work at all (MCP_LANDING_PAGE, on by default).
curl -sI -H 'Accept: text/html' https://lab401.example.com/sas-mcp/mcp | head -1

# 2. The MCP endpoint refuses anonymous calls and names its metadata document.
curl -si -X POST https://lab401.example.com/sas-mcp/mcp \
  -H 'Accept: application/json, text/event-stream' -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' | grep -i www-authenticate

# 3. That document must resolve, and its authorization_servers value must resolve in turn.
curl -s https://lab401.example.com/.well-known/oauth-protected-resource/sas-mcp/mcp | jq
curl -s https://lab401.example.com/.well-known/oauth-authorization-server/sas-mcp | jq .issuer

# 4. And the root-relative form, if rootRelativeOAuth is on.
curl -s https://lab401.example.com/.well-known/oauth-authorization-server | jq .issuer
```

All four working means every client-side discovery path is covered. Then point a client at
`https://lab401.example.com/sas-mcp/mcp` and sign in.

---

## 5. Troubleshooting

| Symptom | Cause |
| --- | --- |
| `kubectl get httpproxy` shows **orphaned** | The root proxy does not include that child. Re-run the patch; check the `namespace` in the include. |
| Root proxy turns **invalid** after the patch | Usually a duplicate include condition — the same prefix added twice. `kubectl -n <viya-ns> get httpproxy sas-httpproxy-root -o yaml` and remove the duplicate. |
| `404` on `/sas-mcp/mcp`, everything else fine | The `replacePrefix` did not fire. Under delegation the child route must carry **no** conditions — the include supplies the prefix, and stating it in both places concatenates to `/sas-mcp/sas-mcp`. |
| Sign-in loops, or the client reports "no authorization server" | A discovery URL is unrouted. Work through the four `curl`s above; the failing one names the missing route. |
| Client reaches `/authorize` but Viya rejects the redirect | The SASLogon client's registered `redirect_uri` lacks the prefix. It must match `MCP_BASE_URL` + `/auth/callback` exactly. |
| Long `execute_sas_code` calls die after ~15s | `timeoutPolicy` missing on the route — Envoy's default response timeout. The chart sets `response: infinity` for you. |
| A browser CSP error during the OAuth redirect | SAS Viya's `form-action` directive does not allow `self`. Patch the Viya CSP configuration before testing the full flow. |
| Uploads fail on large files | Envoy imposes no request-body limit by default, so this is usually `MAX_UPLOAD_BYTES` (100 MiB) or Viya's own limit, not Contour. See `deploy/SCALING.md`. |

---

## 6. Staying on nginx

Nothing was removed. Set `ingress.controller: nginx` and the chart renders the same `Ingress` object
chart 0.1.x did, annotations and all:

```yaml
ingress:
  controller: nginx
  className: nginx
  host: viya.example.com
```

The chart **fails the render** if `ingress.className` is set while the controller is not nginx, so an
unchanged 0.1.x values file cannot quietly become a Contour deployment on upgrade. Path prefixes are
Contour-only; with nginx, give the server its own hostname.
