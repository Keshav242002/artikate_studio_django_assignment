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

## Project Structure

```
├── README.md
├── ANSWERS.md           # Written answers (Sections 1 + 4)
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
```
