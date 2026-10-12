import json
import subprocess
import sys
import warnings
from contextlib import asynccontextmanager
from pathlib import Path

import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from config import ITEM_ID, REDIS_URL, STOCK
from db import ORDER, TAKE, create_pool, tx

HERE = Path(__file__).parent

# stock check, decrement and enqueue as one atomic step
GATE = """
if tonumber(redis.call('GET', KEYS[1]) or '0') <= 0 then return 0 end
redis.call('DECR', KEYS[1])
redis.call('LPUSH', KEYS[2], ARGV[1])
return 1
"""

DECREMENT = "UPDATE items SET available = available - 1, reserved = reserved + 1 WHERE id = %s"

STATUS = {"ok": 200, "accepted": 202, "sold_out": 409, "held": 423, "errors": 500}

pool = stats_pool = rds = gate = None
run_state = {"mode": None, "proc": None, "stock": STOCK}
counters = {}


def reset_counters():
    counters.update(received=0, inflight=0, ok=0, accepted=0, sold_out=0, held=0, errors=0, last="", error="")


async def reset(stock, crash=False, recover=True, hold=0):
    reset_counters()
    run_state["stock"] = stock
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute("TRUNCATE orders")
        await cur.execute(
            "UPDATE items SET total = %s, available = %s, reserved = 0, sold = 0 WHERE id = %s",
            (stock, stock, ITEM_ID),
        )
    await rds.delete(
        "q:orders", "q:processing", "q:expiry", "q:dead", "sold", "w:done", "w:duplicates", "w:last", "w:crash"
    )
    await rds.set("stock", stock)
    await rds.set("w:recover", int(recover))
    await rds.set("w:hold", hold)
    if crash:
        await rds.set("w:crash", 1)


@asynccontextmanager
async def lifespan(app):
    global pool, stats_pool, rds, gate
    pool = await create_pool(50)
    stats_pool = await create_pool(2)
    rds = aioredis.Redis.from_pool(
        aioredis.BlockingConnectionPool.from_url(REDIS_URL, max_connections=50, decode_responses=True)
    )
    gate = rds.register_script(GATE)
    async with pool.acquire() as conn, conn.cursor() as cur:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # "table already exists" notes on every start
            for stmt in (HERE / "schema.sql").read_text().split(";"):
                if stmt.strip():
                    await cur.execute(stmt)
    await reset(STOCK)
    yield
    for p in (pool, stats_pool):
        p.close()
        await p.wait_closed()
    await rds.aclose()


app = FastAPI(lifespan=lifespan)


async def buy_naive(user_id):
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute("SELECT available FROM items WHERE id = %s", (ITEM_ID,))
        (available,) = await cur.fetchone()
        if available <= 0:
            return "sold_out"
        await cur.execute(DECREMENT, (ITEM_ID,))
        await cur.execute(ORDER, (ITEM_ID, user_id, None))
        return "ok"


async def buy_lock(user_id):
    async with tx(pool) as cur:
        await cur.execute("SELECT available FROM items WHERE id = %s FOR UPDATE", (ITEM_ID,))
        (available,) = await cur.fetchone()
        if available <= 0:
            return "sold_out"
        await cur.execute(DECREMENT, (ITEM_ID,))
        await cur.execute(ORDER, (ITEM_ID, user_id, None))
        return "ok"


async def buy_cond(user_id):
    async with tx(pool) as cur:
        if not await cur.execute(TAKE, (ITEM_ID,)):
            return "sold_out"
        await cur.execute(ORDER, (ITEM_ID, user_id, None))
        return "ok"


async def buy_queue(user_id):
    won = await gate(keys=["stock", "q:orders"], args=[f"buy:{user_id}"])
    return "accepted" if won else "sold_out"


async def buy_hold(user_id):
    if await gate(keys=["stock", "q:orders"], args=[f"hold:{user_id}"]):
        return "accepted"
    # nothing free right now: final only once every item is paid for, otherwise a hold may still expire
    sold = int(await rds.get("sold") or 0)
    return "sold_out" if sold >= run_state["stock"] else "held"


MODES = {"naive": buy_naive, "lock": buy_lock, "cond": buy_cond, "queue": buy_queue, "hold": buy_hold}


@app.post("/buy/{mode}")
async def buy(mode: str, user_id: int):
    if mode not in MODES:
        raise HTTPException(404)
    counters["received"] += 1
    counters["inflight"] += 1
    try:
        result = await MODES[mode](user_id)
    except Exception as e:
        result = "errors"
        counters["last"] = f"buyer {user_id}: error"
        counters["error"] = f"{type(e).__name__}: {e}"[:200]
    else:
        counters["last"] = f"buyer {user_id}: {result.replace('_', ' ')}"
    finally:
        counters["inflight"] -= 1
    counters[result] += 1
    return JSONResponse({"result": result}, status_code=STATUS[result])


@app.get("/orders/{user_id}")
async def order_status(user_id: int):
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute("SELECT status FROM orders WHERE item_id = %s AND user_id = %s", (ITEM_ID, user_id))
        row = await cur.fetchone()
    return {"status": row[0] if row else "none"}


@app.post("/webhook/payment")
async def payment_webhook(user_id: int):
    # a real handler first confirms the payment with the gateway; this one trusts the caller
    await rds.lpush("q:orders", f"pay:{user_id}")
    return {"received": True}


class Run(BaseModel):
    mode: str
    stock: int = STOCK
    buyers: int = 1000
    spread: float = 1.0
    crash: bool = False
    recover: bool = True
    hold: float = 2.0
    abandon: float = 50.0


def running():
    proc = run_state["proc"]
    return proc is not None and proc.poll() is None


@app.post("/run")
async def run(body: Run):
    if body.mode not in MODES:
        raise HTTPException(400, "unknown mode")
    if running():
        raise HTTPException(409, "a run is already in progress")
    crash = body.crash and body.mode == "queue"
    name = body.mode + ("_crash_recover" if crash and body.recover else "_crash" if crash else "")
    await reset(body.stock, crash, body.recover, body.hold)
    run_state["mode"] = body.mode
    run_state["proc"] = subprocess.Popen(
        [
            sys.executable, str(HERE / "load.py"),
            body.mode, str(body.buyers), str(body.spread), name, str(body.hold), str(body.abandon),
        ]
    )
    return {"started": body.mode}


@app.get("/stats")
async def stats():
    async with stats_pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute("SELECT available, reserved, sold FROM items WHERE id = %s", (ITEM_ID,))
        available, reserved, sold = await cur.fetchone()
        await cur.execute("SELECT status, COUNT(*) FROM orders GROUP BY status")
        by_status = dict(await cur.fetchall())
    p = rds.pipeline()
    p.get("stock")
    p.llen("q:orders")
    p.llen("q:processing")
    p.zcard("q:expiry")
    p.llen("q:dead")
    p.get("w:alive")
    p.get("w:done")
    p.get("w:duplicates")
    p.get("w:last")
    p.hgetall("results")
    p.hgetall("results:hold")
    stock, queue, processing, expiry, dead, alive, done, duplicates, w_last, results, hold_results = await p.execute()
    pending, paid = by_status.get("pending", 0), by_status.get("paid", 0)
    return {
        "mode": run_state["mode"],
        "running": running(),
        "stock": run_state["stock"],
        "buyers": {
            "got": counters["ok"] + counters["accepted"],
            "sold_out": counters["sold_out"],
            "held": counters["held"],
            "errors": counters["errors"],
            "error": counters["error"],
        },
        "api": {
            "received": counters["received"],
            "inflight": counters["inflight"],
            "last": counters["last"],
        },
        "redis": {
            "stock": int(stock or 0),
            "queue": queue,
            "processing": processing,
            "expiry": expiry,
            "dead": dead,
        },
        "worker": {"alive": int(alive or 0), "done": int(done or 0), "last": w_last or ""},
        "mysql": {
            "available": available,
            "reserved": reserved,
            "sold": sold,
            "orders": sum(by_status.values()),
            "oversold": max(0, pending + paid - run_state["stock"]),
        },
        "payments": {
            "pending": pending,
            "paid": paid,
            "expired": by_status.get("expired", 0),
            "refunded": by_status.get("refunded", 0),
            "duplicates": int(duplicates or 0),
        },
        "results": {m: json.loads(v) for m, v in results.items()},
        "hold_results": {m: json.loads(v) for m, v in hold_results.items()},
    }


@app.get("/")
async def index():
    return FileResponse(HERE / "index.html")
