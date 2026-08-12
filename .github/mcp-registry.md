# Listing this server on the MCP Registry

**Maintainer notes.** Nothing here is needed to run or deploy the server — see
[`../deploy/`](../deploy/README.md) for that. This is about how the container
image gets published to the public [MCP Registry][registry] so MCP clients can
discover and install it without being told about it first, and it sits beside
[`workflows/publish-ghcr.yml`](workflows/publish-ghcr.yml), which implements it.

The registry stores **metadata only** — the artifact stays on GHCR, and this
repo stays the source of truth. There is one package entry and it is the OCI
image, so a user installs the same container this repo already ships. Nothing
new is built or published.

## What the listing tells a client to run

```sh
docker run -i --rm \
  -e VIYA_ENDPOINT=https://viya.example.com \
  -v "$HOME/.sas:/app/.sas" \
  ghcr.io/sassoftware/sas-mcp-server:1.9.1 app-stdio
```

The client supplies `docker run -i --rm` and the image reference itself; the
`-v` mount comes from `runtimeArguments`, the `app-stdio` from
`packageArguments`, and the `-e` values from `environmentVariables`.

Three details in that command are load-bearing:

- **`app-stdio`** overrides the image's `CMD ["app"]`. The default entry point
  starts the uvicorn HTTP server on 8134, which speaks no stdio and would leave
  the client waiting forever. `app-stdio` is a real console script in
  `pyproject.toml`, not something invented for the listing.
- **The mount target must be `/app/.sas`.** `$HOME` is `/app` in the image
  (`adduser --home /app sas`), and stdio mode reads
  `~/.sas/credentials.json` — the cache `sas-viya auth loginCode` writes.
  Mounting anywhere else silently falls through to the device-code flow.
- **stdio, not HTTP.** Local OCI packages are stdio in every published example
  and in every client that installs them. The HTTP deployment is a different
  product — see [`../deploy/README.md`](../deploy/README.md) — and is not what a registry listing
  describes.

Verified against the published `1.9.0` image rather than assumed: driving it
through a real MCP client over stdio registers **75 tools**, loads the token
(`Loaded access token from /app/.sas/credentials.json`) and returns live data
from `list_compute_contexts`.

### Auth is the one rough edge

The mount is marked `isRequired: true`, so clients prompt for it at install
time. That is deliberate. Without a token cache the server falls back to the
RFC 8628 device-code flow, which prints its URL and code to **stderr** — inside
a container, with no browser to open and no TTY, that surfaces only in whatever
log pane the client happens to show. Better to state the prerequisite up front
than to ship an install that appears to hang.

So a user needs the SAS Viya CLI and one `sas-viya auth loginCode` before this
works. That is inherent to stdio mode, not something the listing adds.

## The four moving parts

| Piece | Where | Why |
|---|---|---|
| `server.json` | repo root | The listing itself. `mcp-publisher` reads it from the working directory. |
| `LABEL io.modelcontextprotocol.server.name` | `Dockerfile` | Ownership proof. Must equal `name` in `server.json`. |
| `publish-mcp-registry` job | `.github/workflows/publish-ghcr.yml` | Publishes on `v*` tags, after the image is pushed. |
| OIDC | same job | How the `io.github.sassoftware` namespace is reachable. |

### Why the namespace needs CI

`io.github.<orgname>/*` is granted to a human only if they are an **Owner** of
that GitHub organisation — ordinary membership was deliberately removed as
sufficient. Publishing `io.github.sassoftware/sas-mcp-server` from a laptop
therefore fails for most maintainers.

GitHub Actions OIDC takes a different route: the registry validates the Actions
token and reads its `repository_owner` claim, granting `io.github.<owner>/*`.
A workflow running in `sassoftware/sas-mcp-server` gets the namespace because
of where it runs, with no personal org role and no long-lived token to store.
That is why publishing lives in CI and not in the release checklist.

### Why the job hangs off the image build

The registry does not take ownership on trust. At publish time it fetches
`ghcr.io/sassoftware/sas-mcp-server:<version>` anonymously, reads
`Config.Labels`, and rejects the listing unless
`io.modelcontextprotocol.server.name` matches `name`. So:

- the image must already be pushed → `needs: build-and-push`;
- the image must be **public** — a private package fails with "is private or
  requires authentication";
- multi-arch is fine. The validator resolves the manifest list to a platform
  image before reading the config, so our `linux/amd64` + `linux/arm64` index
  works unchanged.

## Constraints the schema enforces

The [blog post][blog] and the `mcp-publisher init` template both show an npm
package, and copying that shape into an OCI entry fails validation:

- **no `version` field on the package** — the tag goes in `identifier`
  (`ghcr.io/owner/image:1.9.0`). A `version` alongside it is rejected outright.
- **no `registryBaseUrl`** — same reason.
- **`description` is capped at 100 characters.** The `pyproject.toml`
  description is 118 and does not fit.
- `registryType` is **`oci`**, not `docker`.

`ghcr.io` is on the allowlist, along with Docker Hub, Quay, Google Artifact
Registry, Azure Container Registry and MCR. Private registries are not.

## Publishing

Automatic on any `v*` tag, as a second job after the image push. Nothing to run
by hand. The job rewrites `version` and the image tag in `server.json` from the
git tag before publishing, so a forgotten bump cannot publish metadata pointing
at the previous image — the committed values only need to be correct enough to
validate.

To check what landed:

```sh
curl "https://registry.modelcontextprotocol.io/v0/servers?search=io.github.sassoftware/sas-mcp-server"
```

### First publish rides the next release

Every image on GHCR up to and including `1.9.1` was built before the label
existed — confirmed by inspecting them — so publishing `server.json` as it
stands today would fail ownership validation. The first successful publish is
whatever tag first ships an image built from this Dockerfile.

### Getting into the GitHub MCP Registry

Publishing to `registry.modelcontextprotocol.io` is the prerequisite, not the
whole job. Per the [GitHub blog post][blog], inclusion in the GitHub MCP
Registry itself is a separate request — email `partnerships@github.com` and ask.
That is a decision for the maintainers, and this repo does not automate it.

[registry]: https://registry.modelcontextprotocol.io
[blog]: https://github.blog/ai-and-ml/generative-ai/how-to-find-install-and-manage-mcp-servers-with-the-github-mcp-registry/
