# Artikate Studio — Backend Assessment

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py seed_data
python manage.py runserver
```

## Section 1 — Order Summary API

- Endpoint: `GET /api/orders/summary/?user_id=<id>`
- Silk profiler: http://127.0.0.1:8000/silk/

```bash
python manage.py test orders
```
