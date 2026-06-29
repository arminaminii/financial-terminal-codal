import re
import requests
import xml.etree.ElementTree as ET
from django.conf import settings
from crawler.models import CodalCache


# ── XML Parser ──

def extract_tag(block: str, tag: str) -> str:
    m = re.search(rf"<{tag}>([\s\S]*?)</{tag}>", block, re.IGNORECASE)
    if not m:
        return ""
    val = m.group(1)
    val = val.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return val.strip()


def parse_codal_xml(xml_text: str) -> list[dict]:
    """Parse Codal search API XML response → list of letter dicts."""
    results = []
    # Split by <Letter> blocks
    blocks = re.split(r"<Letter\b", xml_text, flags=re.IGNORECASE)
    for block in blocks[1:]:  # skip first (before first <Letter>)
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

def codal_api_url(symbol: str) -> str:
    from urllib.parse import quote
    e = quote(symbol)
    return (
        f"https://search.codal.ir/api/search/v2/q"
        f"?Symbol={e}&LetterType=6&Category=1"
        f"&Audited=true&NotAudited=true&search=true"
        f"&PageNumber=1&Length=-1"
        f"&Mains=true&Childs=true&Publisher=true"
        f"&Consolidatable=true&IsNotAudited=true"
        f"&AuditorRef=-1&CompanyState=0&CompanyType=-1"
    )


# ── Fetch & Parse ──

def normalize(s: str) -> str:
    return re.sub(r"\s+", "", s).strip()


def fetch_and_parse(symbol: str, timeout: int = 15) -> dict:
    """Fetch from codal.ir, parse, and return {reports, company_name, total_raw, method}."""
    url = codal_api_url(symbol)

    # Try direct fetch first
    try:
        resp = requests.get(url, timeout=timeout, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/xml, text/xml, */*",
        })
        if resp.status_code == 200 and len(resp.text) > 50:
            letters = parse_codal_xml(resp.text)
            reports = _filter_and_build(letters, symbol)
            return {
                "reports": reports,
                "company_name": reports[0]["company_name"] if reports else symbol,
                "total_raw": len(letters),
                "method": "direct",
            }
    except requests.RequestException:
        pass

    # Try via proxy if configured
    proxy_config = ProxyConfig.objects.filter(is_active=True).first()
    if proxy_config:
        try:
            proxy_url = proxy_config.proxy_url.rstrip("/")
            fetch_url = f"{proxy_url}?url={requests.utils.quote(url)}"
            resp = requests.get(fetch_url, timeout=timeout)
            if resp.status_code == 200 and len(resp.text) > 50:
                letters = parse_codal_xml(resp.text)
                reports = _filter_and_build(letters, symbol)
                if reports:
                    return {
                        "reports": reports,
                        "company_name": reports[0]["company_name"] if reports else symbol,
                        "total_raw": len(letters),
                        "method": "proxy",
                    }
        except requests.RequestException:
            pass

    return {"reports": [], "company_name": symbol, "total_raw": 0, "method": "failed"}


def _filter_and_build(letters: list[dict], symbol: str) -> list[dict]:
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

import json
from datetime import datetime


def get_cache(symbol: str) -> dict | None:
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


def set_cache(symbol: str, company_name: str, reports: list, total_raw: int, method: str):
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