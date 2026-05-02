import logging

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
)

from .config import BASE_FILE_URL, BASE_URL, BOT_TOKEN
from .handlers import (
    error_handler,
    handle_media,
    list_folders,
    new,
    newfolder,
    post_init,
    start,
    status,
)


logger = logging.getLogger(__name__)


def create_application():
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .base_url(BASE_URL)
        .base_file_url(BASE_FILE_URL)
        .local_mode(True)
        .post_init(post_init)
        .build()
    )

    app.add_error_handler(error_handler)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("new", new))
    app.add_handler(CommandHandler("newfolder", newfolder))
    app.add_handler(CommandHandler("list", list_folders))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(
        MessageHandler(
            (
                filters.PHOTO
                | filters.VIDEO
                | filters.ANIMATION
                | filters.Document.IMAGE
                | filters.Document.VIDEO
            ),
            handle_media,
        )
    )
    return app


def main() -> None:
    app = create_application()
    logger.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)
