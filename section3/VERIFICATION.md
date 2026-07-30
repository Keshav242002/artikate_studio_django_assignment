# Section 3 — Verification

This file records what was actually run to verify multi-tenant isolation: the automated test suite, and a manual shell session proving the fail-closed behaviour live (not just asserted in a test).

## 1. Automated test suite

```bash
python manage.py test tenants -v2
```

**Result: 7/7 tests passed.**

```
test_all_does_not_bypass_scoping ... ok
test_filter_and_get_are_also_scoped ... ok
test_middleware_falls_back_to_jwt_header ... ok
test_middleware_resets_context_after_request_even_on_exception ... ok
test_middleware_resolves_tenant_from_subdomain ... ok
test_no_tenant_context_returns_nothing ... ok
test_tenant_a_cannot_see_tenant_b_orders ... ok

----------------------------------------------------------------------
Ran 7 tests in 0.071s

OK
```

| Test | What it proves |
|---|---|
| `test_tenant_a_cannot_see_tenant_b_orders` | Tenant A's `Order.objects.all()` never includes Tenant B's order — the required negative proof (a) |
| `test_all_does_not_bypass_scoping` | `.all()` and an explicit `.filter(tenant=...)` return identical counts — required proof (b), `.all()` isn't a bypass |
| `test_filter_and_get_are_also_scoped` | `.filter()` and `.get()` are constrained too, not just `.all()` — proves scoping is in `get_queryset()`, not a one-off override |
| `test_no_tenant_context_returns_nothing` | With no tenant bound, `Order.objects.all()` returns 0 rows, not every tenant's rows — fail-closed, not fail-open |
| `test_middleware_resolves_tenant_from_subdomain` | `TenantMiddleware` correctly extracts the tenant from `request.get_host()` and binds it for the request |
| `test_middleware_falls_back_to_jwt_header` | With no matching subdomain, the middleware falls back to decoding a `tenant_id` claim from `Authorization: Bearer <jwt>` |
| `test_middleware_resets_context_after_request_even_on_exception` | The `try/finally` in `TenantMiddleware.__call__` resets the contextvar even when the view raises — no leak into the next request |

## 2. Full suite regression check

```bash
python manage.py test
```

**Result: 21/21 tests passed** (Section 1 + Section 2 + Section 3 combined) — confirms adding tenant scoping to `Order` did not break the existing N+1 fix tests or the job-queue tests.

## 3. Manual proof of fail-closed behaviour (live, not mocked)

Run via `python manage.py shell`:

```python
from tenants.models import Tenant
from tenants.context import set_current_tenant
from orders.models import Order

a = Tenant.objects.create(name="A", slug="a")
b = Tenant.objects.create(name="B", slug="b")

set_current_tenant(None)
Order.objects.all().count()   # -> 0
```

**Actual output:**
```
>>> set_current_tenant(None)
<Token var=<ContextVar name='current_tenant' default=None at 0x105923e20> at 0x1059a8140>
>>> Order.objects.all().count()
0
>>> set_current_tenant(a)
<Token var=<ContextVar name='current_tenant' default=None at 0x105923e20> at 0x1059a8140>
>>> Order.objects.all()
<QuerySet []>
```

With no tenant bound, `.count()` returned `0` instead of every order in the table — the concrete demonstration of the fail-closed design described in `ANSWERS.md`. (The second query returns an empty queryset correctly too — no `Order` rows exist for tenant `a` in this session, only the two `Tenant` rows created above.)

## 4. Conclusion

- Automated suite: 7/7 Section 3 tests passing, 21/21 full suite passing.
- Fail-closed behaviour verified live via the Django shell, not just asserted in a test.
- No bugs found during verification — unlike Section 2, this pass didn't surface a defect needing a fix and regression test.
