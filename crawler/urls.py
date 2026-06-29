from django.urls import path
from crawler import views

urlpatterns = [
    path("api/crawl/", views.crawl_reports, name="crawl"),
    path("api/proxy/", views.proxy_config, name="proxy_config"),
    path("api/proxy/test/", views.proxy_test, name="proxy_test"),
    path("", views.index_page, name="index"),
]