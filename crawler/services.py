import re
import json
import time
from typing import List, Optional, Dict
from urllib.parse import quote
import requests
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
    """Extract total search results count from Codal XML."""
    # Try different possible tags for total count
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

PER_PAGE = 100  # max items per page


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


# ── HTTP Fetcher ─

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "application/xml, text/xml, */*",
    "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
}


def _http_get(url: str, timeout: int = 20) -> Dict:
    """Single HTTP GET with full error info. Returns {ok, status, text, error, latency_ms}."""
    t0 = time.time()
    try:
        resp = requests.get(url, timeout=timeout, headers=HEADERS, verify=True)
        latency = int((time.time() - t0) * 1000)
        return {
            "ok": True,
            "status": resp.status_code,
            "text": resp.text,
            "headers": dict(resp.headers),
            "latency_ms": latency,
            "error": None,
        }
    except requests.exceptions.SSLError as e:
        latency = int((time.time() - t0) * 1000)
        return {"ok": False, "status": 0, "text": "", "latency_ms": latency, "error": f"SSL Error: {str(e)[:200]}"}
    except requests.exceptions.Timeout as e:
        latency = int((time.time() - t0) * 1000)
        return {"ok": False, "status": 0, "text": "", "latency_ms": latency, "error": f"Timeout ({timeout}s)"}
    except requests.exceptions.ConnectionError as e:
        latency = int((time.time() - t0) * 1000)
        return {"ok": False, "status": 0, "text": "", "latency_ms": latency, "error": f"Connection Error: {str(e)[:200]}"}
    except Exception as e:
        latency = int((time.time() - t0) * 1000)
        return {"ok": False, "status": 0, "text": "", "latency_ms": latency, "error": f"Error: {str(e)[:200]}"}


# ── Multi-page Fetch & Parse ──

def normalize(s: str) -> str:
    return re.sub(r"\s+", "", s).strip()


def fetch_all_pages(symbol: str, timeout: int = 20) -> Dict:
    """
    Fetch ALL pages from Codal for a symbol.
    Returns {letters, total_available, pages_fetched, method, debug}.
    """
    all_letters = []
    debug_info = []
    method = "failed"

    def try_direct():
        nonlocal all_letters, method
        # Page 1
        url1 = codal_api_url(symbol, page=1, length=PER_PAGE)
        debug_info.append(f"→ Page 1: {url1[:80]}...")
        r1 = _http_get(url1, timeout)
        debug_info.append(f"  HTTP {r1['status']} | {r1['latency_ms']}ms | {len(r1['text'])} chars")

        if not r1["ok"] or r1["status"] != 200:
            debug_info.append(f"  ✗ {r1['error'] or 'HTTP ' + str(r1['status'])}")
            return False

        if len(r1["text"]) < 20:
            debug_info.append("  ✗ Response too short")
            return False

        letters1 = parse_codal_xml(r1["text"])
        total_count = extract_total_count(r1["text"])
        debug_info.append(f"  Parsed {len(letters1)} letters | TotalCount={total_count}")

        if not letters1:
            debug_info.append("  ✗ No <Letter> blocks found in XML")
            # Show first 300 chars of response for debugging
            debug_info.append(f"  Response preview: {r1['text'][:300]}")
            return False

        all_letters.extend(letters1)

        # If total_count > PER_PAGE, fetch remaining pages
        if total_count > PER_PAGE:
            total_pages = (total_count + PER_PAGE - 1) // PER_PAGE
            debug_info.append(f"  Total={total_count} > {PER_PAGE}, need {total_pages} pages")
            for page in range(2, total_pages + 1):
                url = codal_api_url(symbol, page=page, length=PER_PAGE)
                r = _http_get(url, timeout)
                debug_info.append(f"→ Page {page}: HTTP {r['status']} | {r['latency_ms']}ms | {len(r['text'])} chars")
                if r["ok"] and r["status"] == 200:
                    page_letters = parse_codal_xml(r["text"])
                    all_letters.extend(page_letters)
                    debug_info.append(f"  +{len(page_letters)} letters (total so far: {len(all_letters)})")
                    if len(page_letters) == 0:
                        debug_info.append("  Empty page, stopping")
                        break
                else:
                    debug_info.append(f"  ✗ Failed: {r['error'] or 'HTTP ' + str(r['status'])}")
                    break
                time.sleep(0.3)  # small delay between pages

        method = "direct"
        return True

    def try_proxy():
        nonlocal all_letters, method
        proxy_config = ProxyConfig.objects.filter(is_active=True).first()
        if not proxy_config:
            debug_info.append("No active proxy configured")
            return False

        proxy_base = proxy_config.proxy_url.rstrip("/")
        debug_info.append(f"Trying proxy: {proxy_base[:50]}...")

        url1 = codal_api_url(symbol, page=1, length=PER_PAGE)
        fetch_url = f"{proxy_base}?url={quote(url1)}"
        r1 = _http_get(fetch_url, timeout * 2)
        debug_info.append(f"  Proxy HTTP {r1['status']} | {r1['latency_ms']}ms | {len(r1['text'])} chars")

        if not r1["ok"] or r1["status"] != 200 or len(r1["text"]) < 20:
            debug_info.append(f"  ✗ Proxy failed: {r1['error'] or 'HTTP ' + str(r1['status'])}")
            return False

        letters1 = parse_codal_xml(r1["text"])
        total_count = extract_total_count(r1["text"])
        debug_info.append(f"  Parsed {len(letters1)} letters via proxy | TotalCount={total_count}")

        if not letters1:
            debug_info.append(f"  Proxy response preview: {r1['text'][:300]}")
            return False

        all_letters.extend(letters1)

        if total_count > PER_PAGE:
            total_pages = (total_count + PER_PAGE - 1) // PER_PAGE
            for page in range(2, total_pages + 1):
                url = codal_api_url(symbol, page=page, length=PER_PAGE)
                fetch_url = f"{proxy_base}?url={quote(url)}"
                r = _http_get(fetch_url, timeout * 2)
                debug_info.append(f"→ Proxy Page {page}: HTTP {r['status']} | {r['latency_ms']}ms")
                if r["ok"] and r["status"] == 200:
                    page_letters = parse_codal_xml(r["text"])
                    all_letters.extend(page_letters)
                    debug_info.append(f"  +{len(page_letters)} letters")
                    if len(page_letters) == 0:
                        break
                else:
                    break
                time.sleep(0.3)

        method = "proxy"
        return True

    # Try direct first, then proxy
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
    """Fetch ALL pages from codal.ir, parse, filter, and return final reports."""
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

    # Sort: audited first, then by date desc
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
