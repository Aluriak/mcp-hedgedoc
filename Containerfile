# mcp-hedgedoc -- MCP server for a HedgeDoc 1.x instance.
#
# One image, two variants: HEDGEDOC_READ_ONLY toggles between them at
# container start, so the same image serves both mcp-hedgedoc-ro and
# mcp-hedgedoc-rw (see the compose snippet in README.md).

FROM python:3.12-slim AS build

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir --root-user-action=ignore build && \
    python -m build --wheel --outdir /dist

FROM python:3.12-slim

WORKDIR /app
COPY --from=build /dist/*.whl /tmp/
RUN pip install --no-cache-dir --root-user-action=ignore /tmp/*.whl && \
    rm -rf /tmp/*.whl

# Runs as a non-root user -- nothing here needs root, and there's no
# reason for a compromised dependency to get more than it needs.
RUN useradd --system --no-create-home --uid 10001 mcp-hedgedoc
USER mcp-hedgedoc

ENV PYTHONUNBUFFERED=1
EXPOSE 8000

CMD ["python", "-m", "mcp_hedgedoc"]
