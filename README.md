# Artikate Studio — Backend Assessment

## Requirements

- Python 3.11 (install via pyenv — see below)
- No other external services needed for Section 1

## Setup

```bash
# Install Python 3.11 via pyenv (if not already installed)
curl https://pyenv.run | bash
export PYENV_ROOT="$HOME/.pyenv"
export PATH="$PYENV_ROOT/bin:$PATH"
eval "$(pyenv init -)"
pyenv install 3.11.9

# Create venv with Python 3.11
/Users/$USER/.pyenv/versions/3.11.9/bin/python3.11 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Run migrations
python manage.py migrate

# Seed test data (creates testuser with 250 orders)
python manage.py seed_data

# Run the server
python manage.py runserver
```

## Section 1 — Diagnose a Broken System

**Endpoints:**
- Fixed (3 queries): http://127.0.0.1:8000/api/orders/summary/?user_id=1
- Broken (986 queries): http://127.0.0.1:8000/api/orders/summary-broken/?user_id=1
- Silk profiler: http://127.0.0.1:8000/silk/requests/

**Run tests:**
```bash
python manage.py test orders -v2
```

**Run profiler command (outputs query counts + SQL patterns):**
```bash
python manage.py profile_queries
```

**Files:**
| File | Purpose |
|---|---|
| `orders/models.py` | Order, OrderItem, Product models |
| `orders/serializers.py` | The serializer change that introduced the N+1 |
| `orders/views.py` | Broken and fixed views side by side |
| `orders/tests.py` | `assertNumQueries` proving 986 → 3 queries |
| `section1/INVESTIGATION.md` | Step-by-step incident investigation |
| `section1/silk_requests_list.png` | Silk dashboard showing both endpoints |
| `section1/silk_broken_detail.png` | Silk detail: 986 queries, 1019ms |
| `section1/silk_fixed_detail.png` | Silk detail: 3 queries, 66ms |
| `section1/profiler_output.txt` | Raw profiler output with SQL patterns |
| `ANSWERS.md` | Written answers for all sections |

**Git history (Section 1):**
```
ead6351  Initial project: order summary API endpoint          (you)
9970542  feat: add line items to order summary response       (Arjun Mehta — teammate's bug)
a5073a9  fix: resolve N+1 queries in order summary endpoint   (you — the fix)
```

## Section 2 — Rate-Limited Async Job Queue

Architecture decisions, rate limiter design, and trade-offs are in [`DESIGN.md`](DESIGN.md).

**Run the tests (no Redis or worker process needed):**
```bash
python manage.py test emailqueue -v2
```
Tests use `fakeredis` and Celery's eager mode (`CELERY_TASK_ALWAYS_EAGER`), so the full suite — including the 500-job test — runs in-process with no external services. This includes the required test asserting: no job is lost across 500 submitted jobs, the rate limit (200/window) is never exceeded, and 10 intentionally-failing jobs retry with exponential backoff before landing in the dead-letter table.

**Run it against a real Redis + Celery worker (optional, to see it end-to-end):**
```bash
# Terminal 1 — start Redis
redis-server

# Terminal 2 — start a Celery worker
source venv/bin/activate
celery -A config worker -l info

# Terminal 3 — submit jobs from the Django shell
source venv/bin/activate
python manage.py shell -c "
from emailqueue.tasks import send_email
for i in range(20):
    send_email.delay(f'user{i}@example.com', 'Order confirmation', 'Thanks for your order!')
send_email.delay('user@example.com', 'FAIL: trigger a retry', 'Body')
"
```
Watch the worker log: normal jobs send immediately (up to 200/min), the `FAIL:` job retries 4 times with backoff (1s, 2s, 4s, 8s) before writing to `FailedEmailTask` (visible at `/admin/emailqueue/failedemailtask/`).

**Files:**
| File | Purpose |
|---|---|
| `emailqueue/tasks.py` | `send_email` Celery task — retry, exponential backoff, dead-letter |
| `emailqueue/rate_limiter.py` | `SlidingWindowRateLimiter` — Redis sorted set + WATCH/MULTI/EXEC |
| `emailqueue/models.py` | `FailedEmailTask` — dead-letter store |
| `emailqueue/tests.py` | Rate limiter unit tests + 500-job integration test |
| `config/celery.py` | Celery app definition |
| `DESIGN.md` | Architecture decisions and trade-offs for Section 2 |
| `section2/VERIFICATION.md` | Test run + live Redis/Celery run evidence, including a real bug found and fixed |

## Project Structure

```
├── README.md
├── ANSWERS.md           # Written answers (Sections 1 + 2)
├── DESIGN.md            # Section 2 architecture doc
├── requirements.txt
├── manage.py
├── config/              # Django project settings
├── orders/              # Section 1 — N+1 diagnosis
│   ├── models.py
│   ├── serializers.py
│   ├── views.py
│   ├── urls.py
│   ├── tests.py
│   └── management/commands/
│       ├── seed_data.py
│       └── profile_queries.py
├── section1/
│   ├── INVESTIGATION.md
│   ├── profiler_output.txt
│   ├── silk_requests_list.png
│   ├── silk_broken_detail.png
│   └── silk_fixed_detail.png
├── emailqueue/           # Section 2 — rate-limited async job queue
│   ├── tasks.py
│   ├── rate_limiter.py
│   ├── models.py
│   ├── admin.py
│   └── tests.py
├── section2/
│   └── VERIFICATION.md
```
