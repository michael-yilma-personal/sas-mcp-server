FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS base-builder

WORKDIR /app
COPY . .
# Build the wheel, and export the LOCKED dependency set alongside it. Without
# the export, the runner stage's `pip install <wheel>` resolves dependencies
# fresh against the ranges in pyproject.toml and ignores uv.lock entirely — so
# each rebuild floats to whatever is newest-compatible that day. That silently
# shipped fastmcp 3.4.6 in the v1.8.0 image and 3.4.7 in v1.9.0, and it means
# rebuilding a released tag does not reproduce the released image.
RUN uv build \
    && uv export --frozen --no-default-groups --no-emit-project \
        --format requirements.txt -o /app/dist/requirements.txt

FROM python:3.12-slim-bookworm AS runner
ARG HOST_PORT=8134

LABEL maintainer="david.weik@sas.com"
# Ownership proof for the MCP Registry. It fetches this image anonymously at
# publish time and refuses the listing unless this label equals `name` in
# server.json — it is the only thing stopping someone binding their registry
# entry to an image they do not control. Keep the two in step.
LABEL io.modelcontextprotocol.server.name="io.github.sassoftware/sas-mcp-server"
LABEL org.opencontainers.image.source=https://github.com/sassoftware/sas-mcp-server
LABEL org.opencontainers.image.description="SAS MCP Server — Model Context Protocol server for SAS Viya"
LABEL org.opencontainers.image.licenses=Apache-2.0

RUN addgroup --system sas && adduser --system --ingroup sas --home /app sas

COPY --from=base-builder /app/dist/ /install

WORKDIR /app
# Dependencies from the lock first, then the project wheel with --no-deps so
# pip cannot re-resolve and undo the pinning. The export carries hashes, so pip
# verifies every artifact it downloads.
RUN python3 -m venv .venv \
    && /app/.venv/bin/pip install --no-cache-dir --require-hashes -r /install/requirements.txt \
    && /app/.venv/bin/pip install --no-cache-dir --no-deps /install/*.whl \
    && rm -r /install

ENV PATH="/app/.venv/bin:$PATH"

USER sas

EXPOSE ${HOST_PORT}
CMD ["app"]