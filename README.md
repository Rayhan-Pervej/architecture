# Architecture experiments

Small, runnable experiments for learning backend architecture by measuring it. Each experiment lives in its own folder with its own README.

| Experiment | Question it answers |
|---|---|
| [100items1000buyers](100items1000buyers/) | How do you sell a limited stock to a burst of buyers without overselling, and what does a Redis gate with a queue change? |

## Requirements

- Python 3.12
- Docker Desktop (for Redis and MySQL)

## One-time setup

Run these from this folder, in PowerShell.

**1. Virtual environment and dependencies**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r 100items1000buyers\requirements.txt
```

**2. Redis and MySQL containers**

```powershell
docker run -d --name redis -p 6379:6379 redis
docker run -d --name mysql -p 127.0.0.1:3306:3306 -e MYSQL_ROOT_PASSWORD=root -e MYSQL_DATABASE=queue_demo -v mysql_data:/var/lib/mysql mysql:8.4
```

| Service | Address | Login |
|---|---|---|
| Redis | `localhost:6379` | none |
| MySQL | `localhost:3306`, database `queue_demo` | user `root`, password `root` |

These settings are for local use only. Redis has no password and is published on all network interfaces; MySQL is reachable only from this machine.

## After a reboot

The containers do not start by themselves:

```powershell
docker start redis mysql
```

Then follow the README of the experiment you want to run.
