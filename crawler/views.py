import json
import time
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from crawler.services import fetch_and_parse, get_cache, set_cache, codal_api_url
from crawler.models import ProxyConfig
import requests


@csrf_exempt
def index_page(request):
    return render(request, "crawler/index.html")


def crawl_reports(request):
    """Main crawl endpoint: /api/crawl/?symbol=فولاد"""
    symbol = request.GET.get("symbol", "").strip()
    if not symbol:
        return JsonResponse({"error": True, "message": "نماد را وارد کنید"}, status=400)

    logs = []
    t0 = time.time()

    def log(step, message, status="info"):
        logs.append({
            "step": step,
            "message": message,
            "status": status,
            "timestamp": int((time.time() - t0) * 1000),
        })

    # 1. Check cache
    nocache = request.GET.get("nocache")
    if not nocache:
        log("cache", "بررسی کش دیتابیس...")
        cached = get_cache(symbol)
        if cached:
            log("cache", f"{cached['total_final']} گزارش از کش ✓", "success")
            cached["logs"] = logs
            cached["duration"] = logs[-1]["timestamp"] if logs else 0
            return JsonResponse(cached)
        log("cache", "کش یافت نشد — شروع کرال")
    else:
        log("cache", "کش نادیده گرفته شد")

    # 2. Fetch from codal.ir
    log("fetch", "درخواست به codal.ir (همه صفحات)...")
    result = fetch_and_parse(symbol)
    method = result["method"]
    debug = result.get("debug", [])

    # Add debug lines to logs
    for d in debug:
        status = "success" if "✓" in d or "+" in d.split(" ")[0] else ("error" if "✗" in d else "info")
        log("debug", d, status)

    if result["reports"]:
        log("parse", f"{result['total_raw']} نتیجه خام | {len(result['reports'])} گزارش نهایی", "success")
        set_cache(
            symbol,
            result["company_name"],
            result["reports"],
            result["total_raw"],
            method,
        )
        log("done", f"✓ {len(result['reports'])} گزارش واقعی از کدال ({method})", "success")
        return JsonResponse({
            "symbol": symbol,
            "company_name": result["company_name"],
            "reports": result["reports"],
            "total_raw": result["total_raw"],
            "total_final": len(result["reports"]),
            "crawl_method": method,
            "from_cache": False,
            "logs": logs,
            "duration": logs[-1]["timestamp"] if logs else 0,
        })

    # No reports - return detailed error
    log("done", "خطا در دریافت اطلاعات", "error")
    error_msg = "خطا در دریافت اطلاعات از کدال."
    if method == "failed":
        # Find the actual error from debug
        for d in debug:
            if "✗" in d or "Error" in d or "Timeout" in d or "SSL" in d or "Connection" in d:
                error_msg = d.replace("  ✗ ", "").replace("  ", " ")
                break
        else:
            error_msg = "اتصال به codal.ir برقرار نشد. تنظیمات پروکسی را بررسی کنید یا دوباره تلاش کنید."

    return JsonResponse({
        "symbol": symbol,
        "company_name": symbol,
        "reports": [],
        "total_raw": 0,
        "total_final": 0,
        "crawl_method": method,
        "from_cache": False,
        "crawl_failed": True,
        "message": error_msg,
        "logs": logs,
        "duration": logs[-1]["timestamp"] if logs else 0,
    }, status=503)


@csrf_exempt
@require_http_methods(["GET", "POST", "DELETE"])
def proxy_config(request):
    """Proxy configuration: GET/POST/DELETE /api/proxy/"""
    if request.method == "GET":
        config = ProxyConfig.objects.filter(is_active=True).first()
        if not config:
            return JsonResponse({"configured": False})
        return JsonResponse({
            "configured": True,
            "proxy_url": config.proxy_url,
            "label": config.label,
            "tested_at": config.tested_at.isoformat() if config.tested_at else None,
            "test_ok": config.test_ok,
        })

    elif request.method == "POST":
        data = json.loads(request.body)
        proxy_url = data.get("proxy_url", "").strip()
        label = data.get("label", "").strip()
        if not proxy_url:
            return JsonResponse({"error": True, "message": "آدرس پروکسی الزامی است"}, status=400)
        ProxyConfig.objects.update(is_active=False)
        config = ProxyConfig.objects.create(
            proxy_url=proxy_url, label=label, is_active=True
        )
        return JsonResponse({"success": True, "proxy_url": config.proxy_url})

    elif request.method == "DELETE":
        ProxyConfig.objects.all().delete()
        return JsonResponse({"success": True})


@csrf_exempt
@require_http_methods(["POST"])
def proxy_test(request):
    """Test proxy connectivity to codal.ir"""
    data = json.loads(request.body)
    proxy_url = data.get("proxy_url", "").strip()
    if not proxy_url:
        return JsonResponse({"error": True, "message": "آدرس پروکسی الزامی است"}, status=400)

    test_url = codal_api_url("فولاد")
    base = proxy_url.split("?")[0]
    fetch_url = f"{base}?url={requests.utils.quote(test_url)}"

    t0 = time.time()
    try:
        resp = requests.get(fetch_url, timeout=15)
        latency = int((time.time() - t0) * 1000)
        text = resp.text
        has_data = "codal" in text.lower() or "Letter" in text or len(text) > 500
        ok = resp.status_code == 200 and has_data
        return JsonResponse({
            "ok": ok,
            "status": resp.status_code,
            "body_length": len(text),
            "latency": latency,
            "preview": text[:200],
            "message": f"پروکسی فعال — {latency}ms" if ok else f"پاسخ نامعتبر (HTTP {resp.status_code})",
        })
    except Exception as e:
        latency = int((time.time() - t0) * 1000)
        return JsonResponse({
            "ok": False,
            "latency": latency,
            "message": f"خطا: {str(e)}",
        })


@csrf_exempt
def debug_crawl(request):
    """Debug endpoint: shows raw XML response from codal.ir"""
    symbol = request.GET.get("symbol", "فولاد").strip()
    from crawler.services import codal_api_url, _http_get
    url = codal_api_url(symbol, page=1, length=10)
    r = _http_get(url, timeout=20)
    return JsonResponse({
        "url": url,
        "status": r["status"],
        "latency_ms": r["latency_ms"],
        "body_length": len(r["text"]),
        "body_preview": r["text"][:2000],
        "error": r["error"],
        "has_letter": "<Letter" in r["text"] if r["text"] else False,
    })
