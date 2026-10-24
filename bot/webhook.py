"""
webhook.py — process one Telegram update on a fresh PTB Application.

On Vercel nothing survives between requests, so every update builds its own
Application with NeonPersistence, processes the update and shuts down. The
shutdown in `finally` is what flushes conversation and user_data state to Neon.
"""

from telegram import Update

from persistence import NeonPersistence


async def handle_update(payload: dict) -> None:
    import main  # late import: main imports this module for the route

    app = main.build_application(NeonPersistence())
    await app.initialize()
    try:
        await app.process_update(Update.de_json(payload, app.bot))
    finally:
        await app.shutdown()
