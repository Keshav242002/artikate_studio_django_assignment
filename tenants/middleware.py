import jwt
from django.conf import settings

from tenants.context import reset_current_tenant, set_current_tenant
from tenants.models import Tenant


class TenantMiddleware:
    """
    Resolves the current tenant for the lifetime of a request and binds it
    to the contextvar consumed by TenantManager.

    Resolution order:
      1. Subdomain of the request host (e.g. acme.example.com -> slug "acme")
      2. Fallback: an "Authorization: Bearer <jwt>" header carrying a
         tenant_id claim, signed with SECRET_KEY.

    The reset happens in `finally` so an exception raised by the view can
    never leave a stale tenant bound to a reused worker/context, which would
    leak that tenant into whatever request runs next.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        tenant = self._resolve_tenant(request)
        token = set_current_tenant(tenant)
        try:
            response = self.get_response(request)
        finally:
            reset_current_tenant(token)
        return response

    def _resolve_tenant(self, request):
        host = request.get_host().split(':')[0]
        subdomain = host.split('.')[0]
        tenant = Tenant.objects.filter(slug=subdomain).first()
        if tenant:
            return tenant

        auth_header = request.META.get('HTTP_AUTHORIZATION', '')
        if auth_header.startswith('Bearer '):
            token_str = auth_header.split(' ', 1)[1]
            try:
                payload = jwt.decode(token_str, settings.SECRET_KEY, algorithms=['HS256'])
            except jwt.PyJWTError:
                return None
            return Tenant.objects.filter(pk=payload.get('tenant_id')).first()

        return None
