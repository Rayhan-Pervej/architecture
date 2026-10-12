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

# payment hold mode
PAY_TIME = (0.2, 1.5)       # seconds a paying buyer spends on the payment page
RETRY_AFTER = (1.0, 2.0)    # seconds before a buyer told "all held" tries again
DUPLICATE_WEBHOOKS = 0.2    # share of payments the gateway reports twice
RUN_LIMIT = 180


async def post(session, path, user_id):
    try:
        async with session.post(path, params={"user_id": user_id}) as res:
            await res.read()
            return res.status
    except aiohttp.ClientError:
        return 0


async def buyer(session, mode, user_id, spread):
    await asyncio.sleep(random.uniform(0, spread))
    start = time.perf_counter()
    code = await post(session, f"/buy/{mode}", user_id)
    return code, time.perf_counter() - start


async def shopper(session, user_id, spread, abandon, deadline):
    await asyncio.sleep(random.uniform(0, spread))
    code = await post(session, "/buy/hold", user_id)
    while code == 423 and time.perf_counter() < deadline:
        await asyncio.sleep(random.uniform(*RETRY_AFTER))
        code = await post(session, "/buy/hold", user_id)
    if code != 202:
        return "no item"
    if random.random() * 100 < abandon:
        return "abandoned"
    await asyncio.sleep(random.uniform(*PAY_TIME))
    for _ in range(2 if random.random() < DUPLICATE_WEBHOOKS else 1):
        await post(session, "/webhook/payment", user_id)
    while time.perf_counter() < deadline:
        async with session.get(f"/orders/{user_id}") as res:
            status = (await res.json())["status"]
        if status in ("paid", "refunded"):
            return status
        await asyncio.sleep(0.2)
    return "unknown"


async def get_stats(session):
    async with session.get("/stats") as res:
        return await res.json()


async def run_hold(session, rds, buyers, spread, hold, abandon):
    start = time.perf_counter()
    user_ids = random.sample(range(1, buyers + 1), buyers)
    shoppers = asyncio.gather(*(shopper(session, u, spread, abandon, start + RUN_LIMIT) for u in user_ids))

    stock = (await get_stats(session))["stock"]
    sold_out = None
    while not shoppers.done():
        if sold_out is None and int(await rds.get("sold") or 0) >= stock:
            sold_out = time.perf_counter() - start
        await asyncio.sleep(0.05)
    outcomes = await shoppers

    # let the last unpaid holds expire so the final counts are settled
    stats = await get_stats(session)
    while stats["payments"]["pending"] and time.perf_counter() - start < RUN_LIMIT:
        await asyncio.sleep(0.2)
        stats = await get_stats(session)

    payments = stats["payments"]
    result = {
        "hold": hold,
        "abandon": abandon,
        "stock": stock,
        "buyers": buyers,
        "holds": buyers - outcomes.count("no item"),
        "paid": payments["paid"],
        "expired": payments["expired"],
        "refunded": payments["refunded"],
        "duplicates": payments["duplicates"],
        "unsold": stock - payments["paid"],
        "sold_out_s": round(sold_out, 1) if sold_out else None,
        "run_s": round(time.perf_counter() - start, 1),
    }
    await rds.hset("results:hold", f"{hold:g}|{abandon:g}|{stock}|{buyers}", json.dumps(result))
    return result


async def run_burst(session, rds, mode, buyers, spread, name):
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
    stats = await get_stats(session)
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
    return result


async def main(mode, buyers, spread, name, hold, abandon):
    rds = aioredis.from_url(REDIS_URL, decode_responses=True)
    connector = aiohttp.TCPConnector(limit=MAX_CONNECTIONS)
    async with aiohttp.ClientSession(API_URL, connector=connector) as session:
        if mode == "hold":
            result = await run_hold(session, rds, buyers, spread, hold, abandon)
        else:
            result = await run_burst(session, rds, mode, buyers, spread, name)
    await rds.aclose()
    print(result)


if __name__ == "__main__":
    asyncio.run(
        main(sys.argv[1], int(sys.argv[2]), float(sys.argv[3]), sys.argv[4], float(sys.argv[5]), float(sys.argv[6]))
    )
