import re
import json
import time
import subprocess
import threading
from typing import List, Optional, Dict
from urllib.parse import quote
from crawler.models import CodalCache, ProxyConfig


# ── XML Parser ──

def extract_tag(block: str, tag: str) -> str:
    m = re.search(rf"<{tag}>([\s\S]*?)</{tag}>", block, re.IGNORECASE)
    if not m:
        return ""
    val = m.group(1)
    val = val.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return val.strip()


def parse_codal_xml(xml_text: str) -> List[dict]:
    """Parse Codal search API XML response → list of letter dicts."""
    results = []
    blocks = re.split(r"<Letter\b", xml_text, flags=re.IGNORECASE)
    for block in blocks[1:]:
        letter = {
            "symbol": extract_tag(block, "Symbol"),
            "companyname": extract_tag(block, "CompanyName"),
            "title": extract_tag(block, "Title"),
            "lettercode": extract_tag(block, "LetterCode"),
            "tracingno": extract_tag(block, "TracingNo"),
            "url": extract_tag(block, "Url"),
            "excelurl": extract_tag(block, "ExcelUrl"),
            "publishdatetime": extract_tag(block, "PublishDateTime"),
        }
        if letter["symbol"] or letter["title"]:
            results.append(letter)
    return results


def extract_total_count(xml_text: str) -> int:
    for tag in ["TotalCount", "Total", "Count", "totalcount"]:
        m = re.search(rf"<{tag}>([\s\S]*?)</{tag}>", xml_text, re.IGNORECASE)
        if m:
            try:
                return int(m.group(1).strip())
            except ValueError:
                pass
    return 0


# ── Report Classification ──

def classify_report(title: str) -> dict:
    t = title
    is_audited = "حسابرسی شده" in t or "حسابرسی\u200cشده" in t
    is_consolidated = "تلفیقی" in t
    is_interim = "میانی" in t
    is_annual = "سالانه" in t or "پایان دوره" in t

    period_type = "سایر"
    if is_interim:
        period_type = "میانی"
    elif is_annual:
        period_type = "سالانه"

    report_type = "سایر"
    if "صورت مالی" in t or "صورت\u200cهای مالی" in t or "صورت های مالی" in t:
        report_type = "صورت\u200cهای مالی"
    elif "سود و زیان" in t:
        report_type = "سود و زیان"
    elif "ترازنامه" in t:
        report_type = "ترازنامه"
    elif "جریان نقد" in t or "جریان وجوه نقد" in t:
        report_type = "جریان وجوه نقد"
    elif "تغییرات" in t and "صاحبان" in t:
        report_type = "تغییرات حقوق صاحبان"
    elif "تفسیری" in t:
        report_type = "گزارش تفسیری"

    return {
        "report_type": report_type,
        "period_type": period_type,
        "is_audited": is_audited,
        "is_consolidated": is_consolidated,
    }


# ── Codal API URL Builder ──

PER_PAGE = 100


def codal_api_url(symbol: str, page: int = 1, length: int = PER_PAGE) -> str:
    e = quote(symbol)
    return (
        f"https://search.codal.ir/api/search/v2/q"
        f"?Symbol={e}&LetterType=6&Category=1"
        f"&Audited=true&NotAudited=true&search=true"
        f"&PageNumber={page}&Length={length}"
        f"&Mains=true&Childs=true&Publisher=true"
        f"&Consolidatable=true&IsNotAudited=true"
        f"&AuditorRef=-1&CompanyState=0&CompanyType=-1"
    )


# ── HTTP Fetcher using curl (bypasses TLS fingerprint blocking) ──

def _curl_get(url: str, timeout: int = 20) -> Dict:
    """
    Fetch URL using system curl command.
    This bypasses Python requests' TLS fingerprint that gets blocked by Iranian sites.
    curl has a real browser-like TLS fingerprint.
    """
    t0 = time.time()
    try:
        # Build curl command with browser-like headers
        result = subprocess.run(
            [
                "curl", "-s", "-S",
                "--max-time", str(timeout),
                "--connect-timeout", "10",
                "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                "-H", "Accept: application/xml, text/xml, */*",
                "-H", "Accept-Language: fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7",
                "-H", "Referer: https://search.codal.ir/",
                "-H", "Origin: https://search.codal.ir",
                "-H", "X-Requested-With: XMLHttpRequest",
                "-H", "Connection: keep-alive",
                url
            ],
            capture_output=True,
            text=True,
            timeout=timeout + 5,
        )
        latency = int((time.time() - t0) * 1000)
        text = result.stdout

        if result.returncode != 0:
            err = result.stderr.strip()
            # curl exit code 56 = recv failure, 35 = SSL connect error, 28 = timeout
            if result.returncode == 56:
                return {"ok": False, "status": 0, "text": "", "latency_ms": latency, "error": f"curl: Connection reset (code 56): {err[:150]}"}
            elif result.returncode == 35:
                return {"ok": False, "status": 0, "text": "", "latency_ms": latency, "error": f"curl: SSL error (code 35): {err[:150]}"}
            elif result.returncode == 28:
                return {"ok": False, "status": 0, "text": "", "latency_ms": latency, "error": f"curl: Timeout"}
            elif result.returncode == 6:
                return {"ok": False, "status": 0, "text": "", "latency_ms": latency, "error": f"curl: DNS resolve failed"}
            else:
                return {"ok": False, "status": 0, "text": "", "latency_ms": latency, "error": f"curl error ({result.returncode}): {err[:150]}"}

        # Try to get HTTP status from curl
        # If we got text, consider it ok
        return {
            "ok": True,
            "status": 200,
            "text": text,
            "latency_ms": latency,
            "error": None,
        }
    except subprocess.TimeoutExpired:
        latency = int((time.time() - t0) * 1000)
        return {"ok": False, "status": 0, "text": "", "latency_ms": latency, "error": "curl: Process timeout"}
    except FileNotFoundError:
        latency = int((time.time() - t0) * 1000)
        return {"ok": False, "status": 0, "text": "", "latency_ms": latency, "error": "curl not found! Install curl or use Windows 10+"}
    except Exception as e:
        latency = int((time.time() - t0) * 1000)
        return {"ok": False, "status": 0, "text": "", "latency_ms": latency, "error": f"Error: {str(e)[:200]}"}


def _requests_get_fallback(url: str, timeout: int = 20) -> Dict:
    """Fallback using Python requests (in case curl not available)."""
    import requests
    t0 = time.time()
    try:
        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "Accept": "application/xml, text/xml, */*",
            "Accept-Language": "fa-IR,fa;q=0.9",
            "Referer": "https://search.codal.ir/",
            "Origin": "https://search.codal.ir",
        })
        resp = session.get(url, timeout=timeout, verify=False)
        latency = int((time.time() - t0) * 1000)
        return {"ok": resp.status_code == 200, "status": resp.status_code, "text": resp.text, "latency_ms": latency, "error": None if resp.status_code == 200 else f"HTTP {resp.status_code}"}
    except Exception as e:
        latency = int((time.time() - t0) * 1000)
        return {"ok": False, "status": 0, "text": "", "latency_ms": latency, "error": str(e)[:200]}


def _http_get(url: str, timeout: int = 20, debug_info: list = None) -> Dict:
    """Try curl first, fallback to requests."""
    if debug_info is not None:
        debug_info.append(f"_http_get → trying curl...")
    r = _curl_get(url, timeout)
    if r["ok"] and len(r["text"]) > 20:
        if debug_info is not None:
            debug_info.append(f"  curl OK: {r['latency_ms']}ms | {len(r['text'])} chars")
        return r
    if debug_info is not None:
        debug_info.append(f"  curl failed: {r.get('error', 'unknown')}")
        debug_info.append(f"_http_get → falling back to requests...")
    r2 = _requests_get_fallback(url, timeout)
    if debug_info is not None:
        debug_info.append(f"  requests: {'OK' if r2['ok'] else r2.get('error', 'failed')} | {r2['latency_ms']}ms")
    return r2 if r2["ok"] else r  # prefer the one with more data


# ── Multi-page Fetch & Parse ──

def normalize(s: str) -> str:
    return re.sub(r"\s+", "", s).strip()


def fetch_all_pages(symbol: str, timeout: int = 20) -> Dict:
    all_letters = []
    debug_info = []
    method = "failed"

    def try_direct():
        nonlocal all_letters, method
        url1 = codal_api_url(symbol, page=1, length=PER_PAGE)
        debug_info.append(f"→ [Direct] Page 1 fetch...")
        r1 = _http_get(url1, timeout, debug_info)
        debug_info.append(f"  HTTP {r1['status']} | {r1['latency_ms']}ms | {len(r1['text'])} chars")

        if not r1["ok"] or len(r1["text"]) < 20:
            debug_info.append(f"  ✗ {r1.get('error', 'empty response')}")
            return False

        letters1 = parse_codal_xml(r1["text"])
        total_count = extract_total_count(r1["text"])
        debug_info.append(f"  Parsed {len(letters1)} letters | TotalCount={total_count}")

        if not letters1:
            debug_info.append("  ✗ No <Letter> blocks found in XML")
            debug_info.append(f"  Response preview: {r1['text'][:500]}")
            return False

        all_letters.extend(letters1)

        if total_count > PER_PAGE:
            total_pages = (total_count + PER_PAGE - 1) // PER_PAGE
            debug_info.append(f"  Total={total_count} > {PER_PAGE}, fetching {total_pages} pages")
            for page in range(2, total_pages + 1):
                url = codal_api_url(symbol, page=page, length=PER_PAGE)
                r = _http_get(url, timeout, debug_info)
                debug_info.append(f"→ Page {page}: {r['latency_ms']}ms | {len(r['text'])} chars")
                if r["ok"] and len(r["text"]) > 20:
                    page_letters = parse_codal_xml(r["text"])
                    all_letters.extend(page_letters)
                    debug_info.append(f"  +{len(page_letters)} letters (total: {len(all_letters)})")
                    if len(page_letters) == 0:
                        debug_info.append("  Empty page, stopping")
                        break
                else:
                    debug_info.append(f"  ✗ {r.get('error', 'failed')}")
                    break
                time.sleep(0.5)

        method = "direct"
        return True

    def try_proxy():
        nonlocal all_letters, method
        proxy_config = ProxyConfig.objects.filter(is_active=True).first()
        if not proxy_config:
            debug_info.append("No active proxy configured")
            return False

        proxy_base = proxy_config.proxy_url.rstrip("/")
        debug_info.append(f"Trying proxy: {proxy_base[:60]}...")

        url1 = codal_api_url(symbol, page=1, length=PER_PAGE)
        fetch_url = f"{proxy_base}?url={quote(url1)}"
        r1 = _curl_get(fetch_url, timeout * 2)
        debug_info.append(f"  Proxy: {'OK' if r1['ok'] else r1.get('error', 'failed')} | {r1['latency_ms']}ms | {len(r1['text'])} chars")

        if not r1["ok"] or len(r1["text"]) < 20:
            return False

        letters1 = parse_codal_xml(r1["text"])
        total_count = extract_total_count(r1["text"])
        debug_info.append(f"  Proxy: {len(letters1)} letters | TotalCount={total_count}")

        if not letters1:
            return False

        all_letters.extend(letters1)

        if total_count > PER_PAGE:
            total_pages = (total_count + PER_PAGE - 1) // PER_PAGE
            for page in range(2, total_pages + 1):
                url = codal_api_url(symbol, page=page, length=PER_PAGE)
                fetch_url = f"{proxy_base}?url={quote(url)}"
                r = _curl_get(fetch_url, timeout * 2)
                if r["ok"] and len(r["text"]) > 20:
                    page_letters = parse_codal_xml(r["text"])
                    all_letters.extend(page_letters)
                    debug_info.append(f"  +{len(page_letters)} proxy letters")
                    if len(page_letters) == 0:
                        break
                else:
                    break
                time.sleep(0.5)

        method = "proxy"
        return True

    if try_direct():
        pass
    elif try_proxy():
        pass
    else:
        method = "failed"

    return {
        "letters": all_letters,
        "total_available": len(all_letters),
        "method": method,
        "debug": debug_info,
    }


def fetch_and_parse(symbol: str, timeout: int = 20) -> dict:
    result = fetch_all_pages(symbol, timeout)
    letters = result["letters"]
    reports = _filter_and_build(letters, symbol)
    return {
        "reports": reports,
        "company_name": reports[0]["company_name"] if reports else symbol,
        "total_raw": len(letters),
        "method": result["method"],
        "debug": result["debug"],
    }


def _filter_and_build(letters: List[dict], symbol: str) -> List[dict]:
    norm_sym = normalize(symbol)
    matched = [
        l for l in letters
        if norm_sym in normalize(l.get("symbol", ""))
        or norm_sym in normalize(l.get("companyname", ""))
        or normalize(l.get("symbol", "")) == norm_sym
    ]

    if not matched:
        return []

    company_name = matched[0].get("companyname", symbol)
    reports = []
    for l in matched:
        cls = classify_report(l.get("title", ""))
        raw_url = l.get("url", "")
        url = raw_url if raw_url.startswith("http") else (f"https://codal.ir{raw_url}" if raw_url else "")
        raw_excel = l.get("excelurl", "")
        excel_url = raw_excel if raw_excel.startswith("http") else (f"https://codal.ir{raw_excel}" if raw_excel else "")

        reports.append({
            "title": l.get("title", "بدون عنوان"),
            "url": url,
            "excel_url": excel_url,
            "letter_code": l.get("lettercode", ""),
            "tracing_no": l.get("tracingno", ""),
            "date": l.get("publishdatetime", ""),
            "symbol": l.get("symbol", symbol),
            "company_name": l.get("companyname", company_name),
            "report_type": cls["report_type"],
            "period_type": cls["period_type"],
            "is_audited": cls["is_audited"],
            "is_consolidated": cls["is_consolidated"],
        })

    reports.sort(key=lambda r: (not r["is_audited"], r["date"] or ""))
    return reports


# ── Cache Helpers ──

def get_cache(symbol: str) -> Optional[dict]:
    try:
        entry = CodalCache.objects.filter(symbol__iexact=symbol).first()
        if not entry:
            return None
        return {
            "symbol": entry.symbol,
            "company_name": entry.company_name,
            "reports": json.loads(entry.reports_json),
            "total_raw": entry.total_raw,
            "total_final": entry.total_final,
            "crawl_method": entry.crawl_method,
            "from_cache": True,
            "cached_at": entry.fetched_at.isoformat(),
        }
    except Exception:
        return None


def set_cache(symbol: str, company_name: str, reports: list, total_raw: int, method: str) -> None:
    try:
        CodalCache.objects.update_or_create(
            symbol=symbol.lower(),
            defaults={
                "company_name": company_name,
                "reports_json": json.dumps(reports, ensure_ascii=False),
                "total_raw": total_raw,
                "total_final": len(reports),
                "crawl_method": method,
            },
        )
    except Exception as e:
        print(f"Cache error: {e}")
