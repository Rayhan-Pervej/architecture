from contextlib import asynccontextmanager

import aiomysql

from config import DB

TAKE = "UPDATE items SET available = available - 1, reserved = reserved + 1 WHERE id = %s AND available > 0"
ORDER = "INSERT INTO orders (item_id, user_id, expires_at) VALUES (%s, %s, FROM_UNIXTIME(%s))"

# each status change only succeeds from the status it expects, so running it twice does nothing
MARK_PAID = "UPDATE orders SET status = 'paid' WHERE item_id = %s AND user_id = %s AND status = 'pending'"
MARK_EXPIRED = "UPDATE orders SET status = 'expired' WHERE item_id = %s AND user_id = %s AND status = 'pending'"
MARK_REFUNDED = "UPDATE orders SET status = 'refunded' WHERE item_id = %s AND user_id = %s AND status = 'expired'"
HAS_ORDER = "SELECT 1 FROM orders WHERE item_id = %s AND user_id = %s"
SELL = "UPDATE items SET reserved = reserved - 1, sold = sold + 1 WHERE id = %s"
RELEASE = "UPDATE items SET reserved = reserved - 1, available = available + 1 WHERE id = %s"


def create_pool(size):
    return aiomysql.create_pool(**DB, minsize=1, maxsize=size, autocommit=True)


@asynccontextmanager
async def tx(pool):
    async with pool.acquire() as conn:
        await conn.begin()
        try:
            async with conn.cursor() as cur:
                yield cur
            await conn.commit()
        except BaseException:
            await conn.rollback()
            raise
