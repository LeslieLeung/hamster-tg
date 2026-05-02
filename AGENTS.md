# Repository Guidelines

## Project Structure & Module Organization

`hamster_tg/` contains the Telegram bot package. `app.py` wires the `python-telegram-bot` application, `handlers.py` owns command and media handling, `downloader.py` and `storage.py` manage file retrieval and persistence, and `config.py` centralizes settings. Deployment files live at the repository root: `Dockerfile`, `docker-compose.yml`, `docker-compose.dev.yml`, and `DEPLOY.md`. Runtime output goes to ignored `downloads/` and `telegram-bot-api/` directories. There is currently no committed `tests/` directory.

## Build, Test, and Development Commands

- `uv run python -m hamster_tg`: run the bot locally. Requires `BOT_TOKEN`; for normal use also run against a local Telegram Bot API service.
- `docker compose -f docker-compose.dev.yml up -d --build`: build the local image and start the bot plus `telegram-bot-api`.
- `docker compose up -d`: start the production compose stack using the published image.
- `docker compose down`: stop compose services without deleting saved media.
- `uv run python -m py_compile hamster_tg/*.py`: quick syntax check when no full test suite is available.

Copy `.env.example` to `.env` and fill `BOT_TOKEN`, `TELEGRAM_API_ID`, and `TELEGRAM_API_HASH` before running compose.

## Coding Style & Naming Conventions

Use Python 3.12+ and keep modules small, typed where it improves clarity, with package-relative imports for internal code (`from .config import ...`). Follow PEP 8: four-space indentation, `snake_case` for functions and variables, `UPPER_SNAKE_CASE` for constants, and descriptive async handler names such as `handle_media`. Keep comments sparse; configuration constants belong in `hamster_tg/config.py`.

## Testing Guidelines

No test framework is currently configured. Add tests under `tests/` when changing parsing, storage, deduplication, retry, or handler behavior. Prefer `pytest` naming: files like `tests/test_storage.py`, functions like `test_deduplicates_existing_file()`. Mock Telegram network calls and use temporary directories for storage tests.

## Commit & Pull Request Guidelines

Recent history uses short Conventional Commit-style subjects such as `feat: add /new command` and `refactor: Refactor bot into package structure`. Continue with `feat:`, `fix:`, `refactor:`, `docs:`, or `test:`.

Pull requests should include the behavior change, required environment or deployment changes, and local verification. For bot behavior changes, mention the Telegram command or media workflow exercised. Link issues when applicable; include screenshots only when they clarify chat output.

## Security & Configuration Tips

Never commit `.env`, bot tokens, Telegram API credentials, downloaded media, or `telegram-bot-api/` data. Keep new configuration environment-based, document defaults in `DEPLOY.md` or `README.md`, and avoid hard-coding local paths outside `DOWNLOAD_ROOT`.
