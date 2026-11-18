FROM ghcr.io/astral-sh/uv:0.13.4 AS uv
FROM python:3.12-slim
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy HOST=0.0.0.0 PORT=8081
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --no-default-groups
COPY managed_agents ./managed_agents
COPY registry/agents.json ./registry/agents.json
COPY main.py .
RUN useradd --uid 10001 --create-home app
USER 10001
EXPOSE 8081
CMD ["/app/.venv/bin/python", "main.py"]