# 100 items, 1,000 buyers

A demo of one problem: many buyers try to buy a limited stock at the same moment. It runs the same sale four different ways and shows, live, how many items were really sold and how long buyers waited. A fifth mode adds payment: winners hold an item while they pay, and unpaid holds expire.

The target design is drawn in [architecture.md](architecture.md). This code builds the buying and payment paths of that design. See [Not built](#not-built) for what is missing.

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
2. Press one of the five mode buttons. Each press resets the stock and runs one sale.
3. Watch the six boxes update, then compare the rows in the results tables.

## The four modes

| Mode               | What the API does for each buyer                                                                                       | Result                                    |
| ------------------ | ---------------------------------------------------------------------------------------------------------------------- | ----------------------------------------- |
| No protection      | Reads the stock, then decrements it in a second step                                                                   | Oversells                                 |
| Row lock           | Locks the item row with`SELECT ... FOR UPDATE`, checks, then decrements                                              | Correct; buyers wait in line for the lock |
| Conditional update | One`UPDATE ... WHERE available > 0`                                                                                  | Correct; the simplest safe option         |
| Redis gate + queue | Runs one script in Redis that decrements the stock and queues the winner. Workers write the orders to MySQL afterwards | Correct; buyers are answered far sooner   |

## The crash test

Two checkboxes under the buttons apply to the queue mode:

- **Crash the worker mid-run:** the worker kills itself right after taking a job, before it writes to MySQL.
- **Recover unfinished jobs after a crash:** when the worker restarts, it puts the jobs it was holding back on the queue.

Run the queue mode with the crash on and recovery off, then with both on. The "Lost orders" column shows the difference.

## The payment mode

The "Gate + queue + payment" button runs a longer sale in which getting an item is only a hold:

1. A winner's order is saved as `pending` with an expiry time, and the hold is put on a timer in Redis.
2. Some buyers never pay. The rest spend 0.2 to 1.5 seconds "on the payment page", then the fake gateway calls `/webhook/payment`, which queues a payment job. One payment in five is reported twice.
3. The worker marks the order `paid`, but only if it is still `pending`. A second notification for the same order is ignored.
4. When a hold's time is up and it is still unpaid, the order becomes `expired` and the item goes back on sale in both MySQL and Redis.
5. A payment that arrives after its hold expired is marked `refunded`.
6. Buyers who find everything held are told to retry, and do so every 1 to 2 seconds. They are told "sold out" only when every item is paid for.

Two fields under the buttons control it: **Hold an item for (seconds)** and **Buyers who never pay (%)**. Each combination gets a row in the "Payment hold results" table.

| Column                     | Meaning                                              |
| -------------------------- | ---------------------------------------------------- |
| Holds given                | Buyers who got to hold an item at some point         |
| Paid                       | Orders paid in time; never more than the stock       |
| Expired unpaid             | Holds that ran out and returned their item           |
| Paid late, refunded        | Payments that arrived after the hold had expired     |
| Duplicate payments ignored | Repeat notifications for an order already dealt with |
| Unsold at the end          | Items nobody paid for                                |
| Everything sold after (s)  | Time until every item was paid for                   |

Sample numbers for 100 items and 1,000 buyers where half never pay, from one run each on the same machine as below:

| Hold | Holds given | Paid | Expired unpaid | Paid late, refunded | Everything sold after |
| ---- | ----------- | ---- | -------------- | ------------------- | --------------------- |
| 1 s  | 261         | 100  | 130            | 31                  | 17.1 s                |
| 2 s  | 208         | 100  | 106            | 2                   | 18.1 s                |
| 5 s  | 212         | 100  | 112            | 0                   | 31.6 s                |

A hold shorter than the time buyers need to pay causes refunds; a longer hold keeps unpaid items off sale for longer.

## Reading the results

| Column            | Meaning                                    |
| ----------------- | ------------------------------------------ |
| Got an item       | Buyers the API said yes to                 |
| Orders in MySQL   | Orders really saved                        |
| Oversold          | Orders beyond the stock                    |
| Lost orders       | Buyers who were told yes but have no order |
| All answered (s)  | Time until every buyer had a response      |
| All saved (s)     | Time until every order was in MySQL        |
| Typical wait (ms) | The median buyer's wait for a response     |
| Slowest 1% (ms)   | The wait of the slowest 1% of buyers       |

### Sample numbers

100 items, 1,000 buyers arriving within 1 second, 2 workers. One run on a Ryzen 5 5600 with the load generator, API, Redis and MySQL all on the same machine. Compare the rows with each other; the absolute values will differ on other hardware.

| Mode               | Orders | Oversold | All answered | All saved | Typical wait |
| ------------------ | ------ | -------- | ------------ | --------- | ------------ |
| No protection      | 148    | 48       | 2.80 s       | 2.80 s    | 1,416 ms     |
| Row lock           | 100    | 0        | 2.53 s       | 2.53 s    | 1,641 ms     |
| Conditional update | 100    | 0        | 2.45 s       | 2.45 s    | 1,580 ms     |
| Redis gate + queue | 100    | 0        | 1.00 s       | 1.62 s    | 10 ms        |

With the worker crashed mid-run: 98 orders and 2 lost without recovery, 100 orders and 0 lost with it.

## How the queue mode works

```
API     --- gate script --->  Redis    stock - 1,  q:orders gets the job "buy:42"
Worker  <-- blmove ---------  Redis    job moves from q:orders to q:processing
Worker  --- TAKE + ORDER -->  MySQL    stock - 1,  order row inserted
Worker  --- lrem ----------->  Redis    job removed from q:processing
```

A job is a kind and a buyer ID: `buy:42` saves an order, `hold:42` saves an order that expires, and `pay:42` records a payment.

The API and the worker never call each other. The Redis list is the only link between them. Redis and MySQL each hold their own copy of the stock; MySQL is the final authority, which is why the worker still uses the conditional update.

## Files

| File                              | Purpose                                                                                         |
| --------------------------------- | ----------------------------------------------------------------------------------------------- |
| [app.py](app.py)                   | The API: the five `buy_` functions, the gate script, the order status and payment webhook endpoints, and `/run` and `/stats` |
| [worker.py](worker.py)             | Takes jobs off the Redis queue, writes orders and payments to MySQL, and expires unpaid holds |
| [load.py](load.py)                 | Sends the buyers and records the timings. Started by`/run` as a separate process              |
| [db.py](db.py)                     | The shared SQL statements, the MySQL pool and the transaction helper                            |
| [config.py](config.py)             | Connection settings and the starting stock                                                      |
| [schema.sql](schema.sql)           | The`items` and `orders` tables                                                              |
| [index.html](index.html)           | The page. It polls`/stats` every 300 ms                                                       |
| [architecture.md](architecture.md) | Diagram of the full target design                                                               |

To read the code, start with the four `buy_` functions in `app.py`, then the `work` function in `worker.py`.

## Redis keys

| Key                        | Holds                                                     |
| -------------------------- | --------------------------------------------------------- |
| `stock`                  | Items left, counted down by the gate script               |
| `q:orders`               | Jobs waiting for a worker                                 |
| `q:processing`           | Jobs a worker has taken but not yet committed             |
| `q:expiry`               | Holds on a timer: buyer IDs scored by their expiry time   |
| `q:dead`                 | Jobs that failed with an error                            |
| `sold`                   | Items paid for; decides between "retry" and "sold out"    |
| `w:hold`                 | Hold time for the current run, in seconds                 |
| `w:duplicates`           | Number of repeat payment notifications ignored            |
| `results:hold`           | The payment hold results table                            |
| `w:done`                 | Number of jobs the workers have handled                   |
| `w:last`                 | The workers' last action, as text                         |
| `w:alive`                | Worker count; expires after 3 seconds if the worker stops |
| `w:crash`, `w:recover` | Switches for the crash test                               |
| `results`                | The results table, one entry per mode                     |

## Settings

| Setting                                   | Where                              | Default          |
| ----------------------------------------- | ---------------------------------- | ---------------- |
| MySQL and Redis addresses                 | `config.py`                      | local containers |
| API database connections                  | `create_pool(50)` in `app.py`  | 50               |
| Connections the load generator keeps open | `MAX_CONNECTIONS` in `load.py` | 1,000            |
| How long a run waits for stalled workers  | `STALL_TIMEOUT` in `load.py`   | 10 s             |
| Time a paying buyer takes to pay          | `PAY_TIME` in `load.py`        | 0.2 to 1.5 s     |
| Wait before a buyer retries               | `RETRY_AFTER` in `load.py`     | 1 to 2 s         |
| Share of payments reported twice          | `DUPLICATE_WEBHOOKS` in `load.py` | 0.2           |

`STOCK` in `config.py` only sets the stock shown before the first run. After that the Items field on the page decides it.

## Troubleshooting

- **"No worker is running" banner:** start `python worker.py 2` in the second terminal.
- **A change to `app.py` or `config.py` has no effect:** restart the API. `load.py` and `index.html` are picked up without a restart.
- **Requests fail to connect after several very large runs:** Windows has run out of temporary ports. Wait a minute and run again.

## Not built

These parts of [architecture.md](architecture.md) are not implemented:

- The edge layer: CDN, firewall, rate limiter and load balancer.
- The duplicate-buyer and hold-limit check (step 2).
- A real payment gateway. The load generator plays the gateway, and the webhook does not verify the payment (step 11).
- An actual refund. A late payment is only marked `refunded`.
- Notifications and the outbox (step 14).

Known limits of what is built:

- Redis and MySQL are updated one after the other, not together. A worker that dies between the two leaves them disagreeing, for example an item released in MySQL but not put back on sale in Redis. A real system runs a periodic job that compares them and corrects Redis.
- A buyer whose hold expired cannot buy again, because `orders` allows one order per buyer.

- Recovery assumes one worker program. With several, a starting worker would re-queue jobs another is still handling. The unique key on `orders` keeps that from creating duplicate orders.
- Redis only saves periodic snapshots (its default), so a Redis crash can lose the queue entries and stock changes made since the last one.
- "No recovery" is simulated: the job still moves to `q:processing`, but the restarted worker ignores it.
