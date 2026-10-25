"""Live check of the webhook-state tables against DATABASE_URL (Vercel rehost, Task 2).

Uses update_id values below -1_000_000 and kind 'zz_verify' only, never real ids,
and removes every row it creates. Run from bot/:  python scripts/verify_webhook_state.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402

A, B, C = -1_000_001, -1_000_002, -1_000_003


def check(label: str, ok: bool) -> None:
    print(f"{'OK  ' if ok else 'FAIL'} {label}")
    if not ok:
        raise SystemExit(1)


async def main() -> None:
    pool = await db.init_pool()
    try:
        check("first claim is True", await db.claim_update(A) is True)
        check("second claim of a fresh 'processing' row is False", await db.claim_update(A) is False)
        await db.finish_update(A)
        check("claim after done is False", await db.claim_update(A) is False)

        check("claim B", await db.claim_update(B) is True)
        await db.release_update(B)
        check("claim after release is True", await db.claim_update(B) is True)

        check("claim C", await db.claim_update(C) is True)
        await pool.execute(
            "UPDATE telegram_updates SET received_at = now() - interval '3 minutes' WHERE update_id = $1", C
        )
        check("stale 'processing' row (3 min) is reclaimed", await db.claim_update(C) is True)

        await db.ptb_state_save("zz_verify", "k", {"a": 1, "n": [1, 2]})
        check("ptb_state round-trip", (await db.ptb_state_load("zz_verify")) == {"k": {"a": 1, "n": [1, 2]}})
        await db.ptb_state_delete("zz_verify", "k")
        check("ptb_state delete", (await db.ptb_state_load("zz_verify")) == {})
    finally:
        await pool.execute("DELETE FROM telegram_updates WHERE update_id < -1000000")
        await pool.execute("DELETE FROM ptb_state WHERE kind = 'zz_verify'")
        left = await pool.fetchval(
            "SELECT (SELECT count(*) FROM telegram_updates WHERE update_id < -1000000)"
            " + (SELECT count(*) FROM ptb_state WHERE kind = 'zz_verify')"
        )
        print(f"cleanup: {left} test rows left")
        await db.close_pool()


if __name__ == "__main__":
    asyncio.run(main())
