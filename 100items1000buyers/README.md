# 100 items, 1,000 buyers

A demo of one problem: many buyers try to buy a limited stock at the same moment. It runs the same sale four different ways and shows, live, how many items were really sold and how long buyers waited.

The target design is drawn in [architecture.md](architecture.md). This code builds the buying path of that design (steps 1 and 3 to 7). See [Not built](#not-built) for the rest.

## Run it

Redis and MySQL must be running first; the setup is in the [root README](../README.md).

Open two terminals in this folder, each with the virtual environment activated (`..\.venv\Scripts\Activate.ps1`).

```powershell
uvicorn app:app --port 8000
```

```powershell
python worker.py 2
```

Then open http://127.0.0.1:8000.

The number after `worker.py` is how many workers to run. The worker restarts itself if it dies; Ctrl+C stops it. The API creates the tables on startup, and it drops and recreates `orders` each time.

## Using the page

1. Set **Items**, **Buyers** and **Arrive within (seconds)**. Buyers are sent in random order, each at a random moment inside that window. Use 0 to send them all at once.
2. Press one of the four mode buttons. Each press resets the stock and runs one sale.
3. Watch the five boxes update, then compare the rows in the results table.

## The four modes

| Mode | What the API does for each buyer | Result |
|---|---|---|
| No protection | Reads the stock, then decrements it in a second step | Oversells |
| Row lock | Locks the item row with `SELECT ... FOR UPDATE`, checks, then decrements | Correct; buyers wait in line for the lock |
| Conditional update | One `UPDATE ... WHERE available > 0` | Correct; the simplest safe option |
| Redis gate + queue | Runs one script in Redis that decrements the stock and queues the winner. Workers write the orders to MySQL afterwards | Correct; buyers are answered far sooner |

## The crash test

Two checkboxes under the buttons apply to the queue mode:

- **Crash the worker mid-run:** the worker kills itself right after taking a job, before it writes to MySQL.
- **Recover unfinished jobs after a crash:** when the worker restarts, it puts the jobs it was holding back on the queue.

Run the queue mode with the crash on and recovery off, then with both on. The "Lost orders" column shows the difference.

## Reading the results

| Column | Meaning |
|---|---|
| Got an item | Buyers the API said yes to |
| Orders in MySQL | Orders really saved |
| Oversold | Orders beyond the stock |
| Lost orders | Buyers who were told yes but have no order |
| All answered (s) | Time until every buyer had a response |
| All saved (s) | Time until every order was in MySQL |
| Typical wait (ms) | The median buyer's wait for a response |
| Slowest 1% (ms) | The wait of the slowest 1% of buyers |

### Sample numbers

100 items, 1,000 buyers arriving within 1 second, 2 workers. One run on a Ryzen 5 5600 with the load generator, API, Redis and MySQL all on the same machine. Compare the rows with each other; the absolute values will differ on other hardware.

| Mode | Orders | Oversold | All answered | All saved | Typical wait |
|---|---|---|---|---|---|
| No protection | 148 | 48 | 2.80 s | 2.80 s | 1,416 ms |
| Row lock | 100 | 0 | 2.53 s | 2.53 s | 1,641 ms |
| Conditional update | 100 | 0 | 2.45 s | 2.45 s | 1,580 ms |
| Redis gate + queue | 100 | 0 | 1.00 s | 1.62 s | 10 ms |

With the worker crashed mid-run: 98 orders and 2 lost without recovery, 100 orders and 0 lost with it.

## How the queue mode works

```
API     --- gate script --->  Redis    stock - 1,  q:orders gets the buyer ID
Worker  <-- blmove ---------  Redis    ID moves from q:orders to q:processing
Worker  --- TAKE + ORDER -->  MySQL    stock - 1,  order row inserted
Worker  --- lrem ----------->  Redis    ID removed from q:processing
```

The API and the worker never call each other. The Redis list is the only link between them. Redis and MySQL each hold their own copy of the stock; MySQL is the final authority, which is why the worker still uses the conditional update.

## Files

| File | Purpose |
|---|---|
| [app.py](app.py) | The API: the four `buy_` functions, the gate script, and the `/run` and `/stats` endpoints |
| [worker.py](worker.py) | Takes buyer IDs off the Redis queue and writes the orders to MySQL |
| [load.py](load.py) | Sends the buyers and records the timings. Started by `/run` as a separate process |
| [db.py](db.py) | The shared SQL statements, the MySQL pool and the transaction helper |
| [config.py](config.py) | Connection settings and the starting stock |
| [schema.sql](schema.sql) | The `items` and `orders` tables |
| [index.html](index.html) | The page. It polls `/stats` every 300 ms |
| [architecture.md](architecture.md) | Diagram of the full target design |

To read the code, start with the four `buy_` functions in `app.py`, then the `work` function in `worker.py`.

## Redis keys

| Key | Holds |
|---|---|
| `stock` | Items left, counted down by the gate script |
| `q:orders` | Buyer IDs waiting for a worker |
| `q:processing` | Buyer IDs a worker has taken but not yet committed |
| `q:dead` | Buyer IDs whose save failed with an error |
| `w:done` | Number of jobs the workers have handled |
| `w:last` | The workers' last action, as text |
| `w:alive` | Worker count; expires after 3 seconds if the worker stops |
| `w:crash`, `w:recover` | Switches for the crash test |
| `results` | The results table, one entry per mode |

## Settings

| Setting | Where | Default |
|---|---|---|
| MySQL and Redis addresses | `config.py` | local containers |
| API database connections | `create_pool(50)` in `app.py` | 50 |
| Connections the load generator keeps open | `MAX_CONNECTIONS` in `load.py` | 1,000 |
| How long a run waits for stalled workers | `STALL_TIMEOUT` in `load.py` | 10 s |

`STOCK` in `config.py` only sets the stock shown before the first run. After that the Items field on the page decides it.

## Troubleshooting

- **"No worker is running" banner:** start `python worker.py 2` in the second terminal.
- **A change to `app.py` or `config.py` has no effect:** restart the API. `load.py` and `index.html` are picked up without a restart.
- **Requests fail to connect after several very large runs:** Windows has run out of temporary ports. Wait a minute and run again.

## Not built

These parts of [architecture.md](architecture.md) are not implemented:

- The edge layer: CDN, firewall, rate limiter and load balancer.
- The duplicate-buyer and hold-limit check (step 2).
- Telling the buyer the outcome: order IDs and an order status endpoint.
- Payment, hold expiry and release of unpaid stock (steps 8 to 14).
- Notifications and the outbox.

Known limits of what is built:

- Recovery assumes one worker program. With several, a starting worker would re-queue jobs another is still handling. The unique key on `orders` keeps that from creating duplicate orders.
- Redis only saves periodic snapshots (its default), so a Redis crash can lose the queue entries and stock changes made since the last one.
- "No recovery" is simulated: the job still moves to `q:processing`, but the restarted worker ignores it.
