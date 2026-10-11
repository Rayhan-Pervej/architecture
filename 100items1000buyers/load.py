import asyncio
import json
import random
import sys
import time

import aiohttp
import redis.asyncio as aioredis

from config import API_URL, REDIS_URL

STALL_TIMEOUT = 10
# open connections to the API at once, like a load balancer in front of it; extra buyers wait their turn
MAX_CONNECTIONS = 1000


async def buyer(session, mode, user_id, spread):
    await asyncio.sleep(random.uniform(0, spread))
    start = time.perf_counter()
    try:
        async with session.post(f"/buy/{mode}", params={"user_id": user_id}) as res:
            await res.read()
            code = res.status
    except aiohttp.ClientError:
        code = 0
    return code, time.perf_counter() - start


async def main(mode, buyers, spread, name):
    rds = aioredis.from_url(REDIS_URL, decode_responses=True)
    connector = aiohttp.TCPConnector(limit=MAX_CONNECTIONS)
    async with aiohttp.ClientSession(API_URL, connector=connector) as session:
        user_ids = random.sample(range(1, buyers + 1), buyers)
        start = time.perf_counter()
        out = await asyncio.gather(*(buyer(session, mode, u, spread) for u in user_ids))
        responded = time.perf_counter() - start

        codes = [code for code, _ in out]
        got = codes.count(200) + codes.count(202)
        saved = time.perf_counter()
        if mode == "queue":
            # wait for the workers; give up once they have made no progress for STALL_TIMEOUT
            done = 0
            while done < got and time.perf_counter() - saved < STALL_TIMEOUT:
                now = int(await rds.get("w:done") or 0)
                if now != done:
                    done, saved = now, time.perf_counter()
                await asyncio.sleep(0.005)
        saved -= start
        async with session.get("/stats") as res:
            stats = await res.json()
            mysql = stats["mysql"]

    latency = sorted(t for _, t in out)
    result = {
        "mode": name,
        "stock": stats["stock"],
        "buyers": buyers,
        "spread": spread,
        "got": got,
        "sold_out": codes.count(409),
        "errors": buyers - got - codes.count(409),
        "orders": mysql["orders"],
        "oversold": mysql["oversold"],
        "lost": max(0, got - mysql["orders"]),
        "responded_s": round(responded, 3),
        "saved_s": round(saved, 3),
        "p50_ms": round(latency[len(latency) // 2] * 1000, 1),
        "p99_ms": round(latency[int(len(latency) * 0.99) - 1] * 1000, 1),
    }
    await rds.hset("results", name, json.dumps(result))
    await rds.aclose()
    print(result)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], int(sys.argv[2]), float(sys.argv[3]), sys.argv[4]))
