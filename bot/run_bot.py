"""
Minimal test runner for local development.
Runs the bot in polling mode with only commands.py handlers registered.
Replace with main.py (Session 5) when flows.py and nudges.py are ready.
"""
import logging
import os
from dotenv import load_dotenv
from telegram.ext import ApplicationBuilder

from commands import get_handlers
from flows import get_handlers as get_flow_handlers

load_dotenv()

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
# httpx logs every request URL at INFO, and Telegram API URLs carry the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set in .env")

    app = ApplicationBuilder().token(token).build()

    for handler in get_handlers():
        app.add_handler(handler)

    for handler in get_flow_handlers():
        app.add_handler(handler)

    logger.info("Bot started in polling mode. Press Ctrl+C to stop.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
