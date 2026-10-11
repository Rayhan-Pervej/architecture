from contextlib import asynccontextmanager

import aiomysql

from config import DB

TAKE = "UPDATE items SET available = available - 1, reserved = reserved + 1 WHERE id = %s AND available > 0"
ORDER = "INSERT INTO orders (item_id, user_id) VALUES (%s, %s)"


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
