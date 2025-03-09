# Stage 1: Build Backend
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS backend-builder

# Set the working directory
WORKDIR /mark_consensus

# Copy all project files
ADD . /mark_consensus

# Install dependencies using UV (locked versions)
RUN uv lock && uv sync --frozen && /mark_consensus/.venv/bin/python -c "import web3; print('web3 installed: ', web3.__version__)"

# Stage 2: Final Image
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# Set the working directory in the final container
WORKDIR /app

# Set the Python path
ENV PYTHONPATH=/app/src:/app/src/flare_ai_consensus

# Copy only necessary files from the build stage
COPY --from=backend-builder /mark_consensus/.venv ./.venv
COPY --from=backend-builder /mark_consensus/src ./src
COPY --from=backend-builder /mark_consensus/pyproject.toml .
COPY --from=backend-builder /mark_consensus/README.md .
COPY --from=backend-builder /mark_consensus/src/flare_ai_consensus/abi.json ./src/flare_ai_consensus/abi.json

# Expose port 80 (optional, only if needed for networking)
EXPOSE 80

LABEL "tee.launch_policy.allow_env_override"="OPEN_ROUTER_API_KEY"
LABEL "tee.launch_policy.log_redirect"="always"

CMD ["/app/.venv/bin/python", "/app/src/flare_ai_consensus/nft_monitor.py"]
