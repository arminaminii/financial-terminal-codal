from django.db import models


class CodalCache(models.Model):
    symbol = models.CharField(max_length=100, unique=True)
    company_name = models.CharField(max_length=300, default="")
    reports_json = models.TextField(default="[]")
    total_raw = models.IntegerField(default=0)
    total_final = models.IntegerField(default=0)
    crawl_method = models.CharField(max_length=50, default="")
    fetched_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "codal_cache"
        ordering = ["-fetched_at"]

    def __str__(self):
        return f"{self.symbol} ({self.total_final} reports)"


class ProxyConfig(models.Model):
    proxy_url = models.URLField(unique=True)
    label = models.CharField(max_length=100, default="")
    is_active = models.BooleanField(default=True)
    tested_at = models.DateTimeField(null=True, blank=True)
    test_ok = models.BooleanField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "proxy_config"

    def __str__(self):
        return f"{self.label or self.proxy_url}"
