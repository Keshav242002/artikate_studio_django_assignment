from django.db import models

from tenants.context import get_current_tenant


class TenantManager(models.Manager):
    """
    Scopes every queryset to the tenant bound in the current context.

    get_queryset() is overridden (not all()) because every other manager
    method — filter(), get(), first(), etc. — routes through get_queryset()
    internally. Overriding all() alone would leave those other entry points
    unscoped, which is exactly the bypass this manager exists to prevent.

    Fails closed: with no tenant bound, callers get an empty queryset rather
    than every tenant's rows. A forgotten context-set is a broken feature,
    not a data breach.
    """

    def get_queryset(self):
        tenant = get_current_tenant()
        qs = super().get_queryset()
        if tenant is None:
            return qs.none()
        return qs.filter(tenant=tenant)
