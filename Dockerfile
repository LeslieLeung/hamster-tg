FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
WORKDIR /app
COPY pyproject.toml uv.lock .
RUN uv sync --frozen --no-dev --no-install-project
COPY hamster_tg ./hamster_tg
CMD ["uv", "run", "--no-sync", "python", "-m", "hamster_tg"]
