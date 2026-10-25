"""Apply one SQL migration file to DATABASE_URL.  Run from bot/:
    python scripts/apply_migration.py migrations/2026-10-24-webhook-state.sql
"""

import asyncio
import os
import sys
from pathlib import Path

import asyncpg
from dotenv import load_dotenv


async def main(path: str) -> None:
    load_dotenv()
    sql = Path(path).read_text(encoding="utf-8")
    conn = await asyncpg.connect(os.environ["DATABASE_URL"], statement_cache_size=0)
    try:
        await conn.execute(sql)
        print(f"applied {path}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
