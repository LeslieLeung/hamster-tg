FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
WORKDIR /app
COPY pyproject.toml .
RUN uv sync --no-dev --no-install-project
COPY hamster_tg ./hamster_tg
CMD ["uv", "run", "python", "-m", "hamster_tg"]
