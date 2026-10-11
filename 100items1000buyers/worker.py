import asyncio
import os
import subprocess
import sys
import time

import redis.asyncio as aioredis
from pymysql.err import IntegrityError

from config import ITEM_ID, REDIS_URL
from db import ORDER, TAKE, create_pool, tx


async def work(name, pool, rds):
    while True:
        # the job stays in q:processing until its order is committed
        job = await rds.blmove("q:orders", "q:processing", 1, "RIGHT", "LEFT")
        if not job:
            continue
        user_id = int(job)
        if await rds.getdel("w:crash"):
            await rds.set("w:last", f"{name}: crashed while holding buyer {user_id}")
            os._exit(1)
        try:
            async with tx(pool) as cur:
                saved = await cur.execute(TAKE, (ITEM_ID,))
                if saved:
                    await cur.execute(ORDER, (ITEM_ID, user_id))
            last = f"{name}: order saved for buyer {user_id}" if saved else f"{name}: no stock for buyer {user_id}"
        except IntegrityError:
            last = f"{name}: buyer {user_id} already has an order"
        except Exception as e:
            await rds.lpush("q:dead", user_id)
            last = f"{name}: failed for buyer {user_id} ({type(e).__name__})"
        async with rds.pipeline(transaction=True) as p:
            p.lrem("q:processing", 1, user_id)
            p.incr("w:done")
            p.set("w:last", last)
            await p.execute()


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
    pool = await create_pool(count)
    rds = aioredis.from_url(REDIS_URL, decode_responses=True)
    await recover(rds)
    print(f"{count} workers waiting for jobs")
    await asyncio.gather(heartbeat(rds, count), *(work(f"worker-{i + 1}", pool, rds) for i in range(count)))


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
