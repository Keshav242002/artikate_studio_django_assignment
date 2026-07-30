from contextvars import ContextVar

_current_tenant: ContextVar = ContextVar("current_tenant", default=None)


def set_current_tenant(tenant):
    return _current_tenant.set(tenant)


def get_current_tenant():
    return _current_tenant.get()


def reset_current_tenant(token):
    _current_tenant.reset(token)
