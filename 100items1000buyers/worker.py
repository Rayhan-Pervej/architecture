import asyncio
import os
import subprocess
import sys
import time

import redis.asyncio as aioredis
from pymysql.err import IntegrityError

from config import ITEM_ID, REDIS_URL
from db import HAS_ORDER, MARK_EXPIRED, MARK_PAID, MARK_REFUNDED, ORDER, RELEASE, SELL, TAKE, create_pool, tx


async def place(pool, rds, user_id, hold):
    expires = None
    if hold:
        # scheduled before the order exists: expiring an order that was never saved does nothing
        expires = time.time() + float(await rds.get("w:hold"))
        await rds.zadd("q:expiry", {user_id: expires})
    async with tx(pool) as cur:
        saved = await cur.execute(TAKE, (ITEM_ID,))
        if saved:
            await cur.execute(ORDER, (ITEM_ID, user_id, expires))
    return "order saved" if saved else "no stock"


async def pay(pool, user_id):
    for _ in range(20):
        async with tx(pool) as cur:
            if await cur.execute(MARK_PAID, (ITEM_ID, user_id)):
                await cur.execute(SELL, (ITEM_ID,))
                return "paid"
            if await cur.execute(MARK_REFUNDED, (ITEM_ID, user_id)):
                return "paid late, refunded"
            if await cur.execute(HAS_ORDER, (ITEM_ID, user_id)):
                return "duplicate payment ignored"
        # no order yet: another worker may still be saving it
        await asyncio.sleep(0.05)
    raise LookupError("payment for an order that does not exist")


async def work(name, pool, rds):
    while True:
        # the job stays in q:processing until its work is committed
        job = await rds.blmove("q:orders", "q:processing", 1, "RIGHT", "LEFT")
        if not job:
            continue
        kind, user_id = job.split(":")
        user_id = int(user_id)
        if await rds.getdel("w:crash"):
            await rds.set("w:last", f"{name}: crashed while holding buyer {user_id}")
            os._exit(1)
        try:
            if kind == "pay":
                outcome = await pay(pool, user_id)
            else:
                outcome = await place(pool, rds, user_id, hold=kind == "hold")
        except IntegrityError:
            outcome = "already has an order"
        except Exception as e:
            await rds.lpush("q:dead", job)
            outcome = f"failed ({type(e).__name__})"
        async with rds.pipeline(transaction=True) as p:
            p.lrem("q:processing", 1, job)
            p.incr("w:done")
            p.set("w:last", f"{name}: buyer {user_id} {outcome}")
            if outcome == "paid":
                p.incr("sold")
                p.zrem("q:expiry", user_id)
            elif outcome == "duplicate payment ignored":
                p.incr("w:duplicates")
            await p.execute()


async def expire(pool, rds):
    while True:
        due = await rds.zrangebyscore("q:expiry", 0, time.time(), start=0, num=100)
        for user_id in map(int, due):
            async with tx(pool) as cur:
                released = await cur.execute(MARK_EXPIRED, (ITEM_ID, user_id))
                if released:
                    await cur.execute(RELEASE, (ITEM_ID,))
            async with rds.pipeline(transaction=True) as p:
                p.zrem("q:expiry", user_id)
                if released:
                    p.incr("stock")
                    p.set("w:last", f"expiry: buyer {user_id} did not pay, item back on sale")
                await p.execute()
        if not due:
            await asyncio.sleep(0.1)


async def recover(rds):
    stranded = await rds.llen("q:processing")
    if not stranded:
        return
    if await rds.get("w:recover") == "1":
        while await rds.lmove("q:processing", "q:orders", "RIGHT", "RIGHT"):
            pass
        last = f"restarted: put {stranded} unfinished job(s) back on the queue"
    else:
        last = f"restarted: {stranded} unfinished job(s) left stranded"
    await rds.set("w:last", last)
    print(last)


async def heartbeat(rds, count):
    while True:
        await rds.set("w:alive", count, ex=3)
        await asyncio.sleep(1)


async def main(count):
    pool = await create_pool(count + 1)
    rds = aioredis.from_url(REDIS_URL, decode_responses=True)
    await recover(rds)
    print(f"{count} workers waiting for jobs")
    await asyncio.gather(
        heartbeat(rds, count),
        expire(pool, rds),
        *(work(f"worker-{i + 1}", pool, rds) for i in range(count)),
    )


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--child"]
    count = args[0] if args else "2"
    try:
        if "--child" in sys.argv:
            asyncio.run(main(int(count)))
        else:
            # restart the worker when it dies, as Docker or Kubernetes would
            while subprocess.call([sys.executable, "-u", __file__, count, "--child"]):
                print("worker crashed, restarting")
                time.sleep(1)
    except KeyboardInterrupt:
        pass
