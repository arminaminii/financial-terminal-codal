from django.contrib import admin
from .models import CodalCache, ProxyConfig

@admin.register(CodalCache)
class CodalCacheAdmin(admin.ModelAdmin):
    list_display = ('symbol', 'company_name', 'total_final', 'crawl_method', 'fetched_at')
    search_fields = ('symbol', 'company_name')
    list_filter = ('crawl_method',)

@admin.register(ProxyConfig)
class ProxyConfigAdmin(admin.ModelAdmin):
    list_display = ('proxy_url', 'label', 'is_active', 'test_ok', 'tested_at')
