#!/usr/bin/env python3
"""
SEC Insider Cluster Buy Scanner
Pulls Form 4 filings from SEC EDGAR, identifies clusters of insider buying.
"""

import json
import os
import re
import smtplib
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

import requests
import xml.etree.ElementTree as ET

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
EDGAR_BASE = "https://data.sec.gov"
EDGAR_FULL = "https://www.sec.gov"
HEADERS = {"User-Agent": "InsiderScanner joshuachen1208@gmail.com"}

CLUSTER_MIN_INSIDERS = 2          # flag if ≥ N insiders buy within window
CLUSTER_WINDOW_DAYS = 5           # look-back window in days
LOOKBACK_DAYS = 3                 # how far back to pull filings

C_SUITE_KEYWORDS = {
    "ceo", "chief executive",
    "cfo", "chief financial",
    "coo", "chief operating",
    "cto", "chief technology",
    "cso", "chief strategy",
    "cmo", "chief marketing",
    "president",
    "director",
    "chairman",
    "vice president", "vp",
    "executive vice",
    "svp", "evp",
    "principal",
    "general counsel",
    "secretary",
}

OUTPUT_JSON = "data/results.json"
TOP_N_EMAIL = 3
DEBUG_SAMPLES = 5  # print raw XML + parsed fields for first N successful fetches


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def is_c_suite(title: str) -> bool:
    t = title.lower()
    return any(kw in t for kw in C_SUITE_KEYWORDS)


def safe_float(val) -> float:
    try:
        return float(val) if val else 0.0
    except (ValueError, TypeError):
        return 0.0


def signal_strength(cluster: dict) -> str:
    """Return a human-readable signal strength label."""
    score = cluster["insider_count"] * 2 + min(cluster["total_shares"] / 10_000, 5)
    score += min(cluster["total_dollars"] / 500_000, 5)
    if score >= 12:
        return "VERY STRONG"
    if score >= 8:
        return "STRONG"
    if score >= 5:
        return "MODERATE"
    return "WEAK"


# ---------------------------------------------------------------------------
# SEC EDGAR fetch
# ---------------------------------------------------------------------------

def get_recent_form4_accessions(days_back: int = LOOKBACK_DAYS) -> list[dict]:
    """Fetch recent Form 4 filing index entries from EDGAR full-text search."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%d")
    url = (
        f"{EDGAR_BASE}/submissions/CIK.json"  # placeholder – we use the search endpoint
    )

    # Use EDGAR EFTS (full-text search) for recent Form 4s
    efts_url = "https://efts.sec.gov/LATEST/search-index?q=%22form+4%22&dateRange=custom"
    search_url = (
        f"https://efts.sec.gov/LATEST/search-index?forms=4"
        f"&dateRange=custom&startdt={cutoff}&enddt=9999-12-31&_source=file_date,entity_name,file_num,period_of_report,form_type&hits.hits.total.value=true&hits.hits._source=true&hits.hits.highlight=false&hits.hits._id=true"
    )

    # EDGAR full-text search API
    search_api = (
        f"https://efts.sec.gov/LATEST/search-index?forms=4&dateRange=custom"
        f"&startdt={cutoff}&hits.hits._source=file_date,period_of_report,entity_name,file_num,accession_no,period_of_report"
    )

    # Use the proper EDGAR search endpoint
    proper_url = (
        f"https://efts.sec.gov/LATEST/search-index?forms=4"
        f"&dateRange=custom&startdt={cutoff}"
    )

    print(f"Fetching Form 4 filings since {cutoff} ...")
    results = []
    start = 0
    page_size = 100

    _max_retries = 3
    _retry_base_delay = 5  # seconds; doubles each attempt (5, 10, 20)

    while True:
        resp = None
        for attempt in range(_max_retries + 1):
            try:
                resp = requests.get(
                    "https://efts.sec.gov/LATEST/search-index",
                    params={
                        "forms": "4",
                        "dateRange": "custom",
                        "startdt": cutoff,
                        "hits.hits.total.value": "true",
                        "_source": "file_date,period_of_report,entity_name,accession_no",
                        "from": start,
                        "size": page_size,
                    },
                    headers=HEADERS,
                    timeout=30,
                )
                resp.raise_for_status()
                break
            except requests.RequestException as e:
                if attempt < _max_retries:
                    delay = _retry_base_delay * (2 ** attempt)
                    print(f"  EDGAR error (attempt {attempt + 1}/{_max_retries}): {e} — retrying in {delay}s ...")
                    time.sleep(delay)
                else:
                    print(f"  EDGAR API failed after {_max_retries} retries: {e}")
                    return []
        data = resp.json()
        hits = data.get("hits", {}).get("hits", [])
        if not hits:
            break
        for hit in hits:
            src = hit.get("_source", {})
            accession = hit.get("_id", "").replace(":", "-")
            if not accession:
                accession = src.get("accession_no", "")
            results.append({
                "accession_no": accession,
                "entity_name": src.get("entity_name", ""),
                "file_date": src.get("file_date", ""),
                "period_of_report": src.get("period_of_report", ""),
            })
        total = data.get("hits", {}).get("total", {}).get("value", 0)
        start += page_size
        if start >= total or start >= 2000:  # cap at 2000 for speed
            break
        time.sleep(0.2)

    print(f"  Found {len(results)} Form 4 accessions")
    return results


def fetch_filing_xml(accession_no: str) -> Optional[str]:
    """Download the primary XML document for a Form 4 accession."""
    clean = accession_no.replace("-", "")
    cik_part = clean[:10]
    cik_int = int(cik_part)
    acc_nodash = clean  # e.g. 000123456724000001

    # Primary guess: {accession_no}.xml
    xml_url = f"{EDGAR_FULL}/Archives/edgar/data/{cik_int}/{acc_nodash}/{accession_no}.xml"
    try:
        resp = requests.get(xml_url, headers=HEADERS, timeout=15)
        if resp.status_code == 200 and "<ownershipDocument>" in resp.text:
            return resp.text
    except requests.RequestException:
        pass

    # Fallback: parse filing index page to find the correct XML filename
    for idx_suffix in (f"{accession_no}-index.htm", f"{accession_no}-index.html", ""):
        idx_url = f"{EDGAR_FULL}/Archives/edgar/data/{cik_int}/{acc_nodash}/{idx_suffix}"
        try:
            idx_resp = requests.get(idx_url, headers=HEADERS, timeout=15)
            if idx_resp.status_code != 200:
                continue
            # Collect all .xml hrefs, prefer those containing "ownershipDocument" hint
            xml_candidates = []
            for line in idx_resp.text.splitlines():
                m = re.search(r'href="([^"]+\.xml)"', line, re.IGNORECASE)
                if m:
                    href = m.group(1)
                    full = f"{EDGAR_FULL}{href}" if href.startswith("/") else f"{EDGAR_FULL}/Archives/edgar/data/{cik_int}/{acc_nodash}/{href}"
                    xml_candidates.append(full)
            for url in xml_candidates:
                try:
                    r2 = requests.get(url, headers=HEADERS, timeout=15)
                    if r2.status_code == 200 and "<ownershipDocument>" in r2.text:
                        return r2.text
                except requests.RequestException:
                    continue
            if xml_candidates:
                break  # tried the index page, don't loop further
        except requests.RequestException:
            continue

    return None


def parse_form4(xml_text: str, meta: dict, debug: bool = False) -> list[dict]:
    """Parse Form 4 XML and return list of transaction records."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        if debug:
            print(f"  [parse] XML parse error: {e}")
        return []

    def find_text(element, path):
        el = element.find(path)
        return el.text.strip() if el is not None and el.text else ""

    # Issuer info
    issuer = root.find("issuer")
    ticker = find_text(issuer, "issuerTradingSymbol") if issuer is not None else ""
    company = find_text(issuer, "issuerName") if issuer is not None else meta.get("entity_name", "")

    # Reporting owner — Form 4 can have multiple; iterate all
    all_owner_els = root.findall("reportingOwner")
    if not all_owner_els:
        if debug:
            print(f"  [parse] no <reportingOwner> found in {meta.get('accession_no','?')}")
        return []

    records = []
    for owner_el in all_owner_els:
        owner_id = owner_el.find("reportingOwnerId")
        owner_rel = owner_el.find("reportingOwnerRelationship")

        buyer_name = ""
        if owner_id is not None:
            buyer_name = find_text(owner_id, "rptOwnerName")

        role = ""
        if owner_rel is not None:
            is_director = find_text(owner_rel, "isDirector") == "1"
            is_officer = find_text(owner_rel, "isOfficer") == "1"
            officer_title = find_text(owner_rel, "officerTitle")
            if is_director and is_officer:
                role = officer_title or "Director/Officer"
            elif is_director:
                role = officer_title or "Director"
            elif is_officer:
                role = officer_title or "Officer"
            elif officer_title:
                # Flag set to 0 but title is filled — trust the title
                role = officer_title

        if debug:
            print(f"  [parse] owner={buyer_name!r:40s}  role={role!r}")

        if not is_c_suite(role):
            if debug:
                print(f"           → skipped (not C-suite/director)")
            continue

        # Non-derivative transactions
        all_txn_codes = []
        for txn in root.findall(".//nonDerivativeTransaction"):
            code = find_text(txn, "transactionCoding/transactionCode")
            all_txn_codes.append(code)
            if code != "P":  # open-market purchase only
                continue

            shares_str = find_text(txn, "transactionAmounts/transactionShares/value")
            price_str  = find_text(txn, "transactionAmounts/transactionPricePerShare/value")
            date_str   = find_text(txn, "transactionDate/value") or meta.get("period_of_report", "")
            security   = find_text(txn, "securityTitle/value")

            shares  = safe_float(shares_str)
            price   = safe_float(price_str)
            dollars = shares * price

            records.append({
                "ticker": ticker,
                "company": company,
                "buyer_name": buyer_name,
                "role": role,
                "transaction_date": date_str,
                "file_date": meta.get("file_date", ""),
                "shares": shares,
                "price_per_share": price,
                "total_dollars": dollars,
                "security": security,
                "accession_no": meta.get("accession_no", ""),
                "transaction_code": code,
            })

        if debug:
            print(f"           → txn codes found: {all_txn_codes or '(none)'}, P-purchases added: {sum(1 for r in records if r['buyer_name'] == buyer_name)}")

    return records


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------

def cluster_transactions(transactions: list[dict]) -> list[dict]:
    """Group transactions by ticker/company and flag clusters."""
    by_company = defaultdict(list)
    for txn in transactions:
        key = txn["ticker"] or txn["company"]
        by_company[key].append(txn)

    clusters = []
    for key, txns in by_company.items():
        if len(txns) < CLUSTER_MIN_INSIDERS:
            continue

        # Check if ≥2 unique insiders bought within CLUSTER_WINDOW_DAYS of each other
        dates = []
        for txn in txns:
            try:
                dates.append(datetime.strptime(txn["transaction_date"], "%Y-%m-%d"))
            except ValueError:
                pass

        if not dates:
            continue

        dates.sort()
        span = (dates[-1] - dates[0]).days
        if span > CLUSTER_WINDOW_DAYS:
            # Find largest window with ≥ CLUSTER_MIN_INSIDERS insiders
            valid = False
            for i in range(len(dates)):
                for j in range(i + 1, len(dates)):
                    if (dates[j] - dates[i]).days <= CLUSTER_WINDOW_DAYS:
                        valid = True
            if not valid:
                continue

        unique_insiders = {t["buyer_name"] for t in txns}
        if len(unique_insiders) < CLUSTER_MIN_INSIDERS:
            continue

        total_shares = sum(t["shares"] for t in txns)
        total_dollars = sum(t["total_dollars"] for t in txns)
        ticker = txns[0]["ticker"] or key
        company = txns[0]["company"] or key

        cluster = {
            "ticker": ticker,
            "company": company,
            "insider_count": len(unique_insiders),
            "insiders": [
                {"name": t["buyer_name"], "role": t["role"], "shares": t["shares"], "dollars": t["total_dollars"], "date": t["transaction_date"]}
                for t in txns
            ],
            "total_shares": total_shares,
            "total_dollars": total_dollars,
            "date_range": f"{min(t['transaction_date'] for t in txns)} – {max(t['transaction_date'] for t in txns)}",
            "transactions": txns,
        }
        cluster["signal_strength"] = signal_strength(cluster)
        clusters.append(cluster)

    # Sort by total dollars descending
    clusters.sort(key=lambda c: c["total_dollars"], reverse=True)
    return clusters


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def send_email_digest(clusters: list[dict]):
    """Send top-N clusters as HTML email via Gmail SMTP."""
    smtp_user = os.environ.get("GMAIL_USER", "")
    smtp_pass = os.environ.get("GMAIL_APP_PASSWORD", "")
    recipient = os.environ.get("RECIPIENT_EMAIL", "joshuachen1208@gmail.com")

    if not smtp_user or not smtp_pass:
        print("GMAIL_USER / GMAIL_APP_PASSWORD not set – skipping email.")
        return

    top = clusters[:TOP_N_EMAIL]
    today = datetime.now().strftime("%B %d, %Y")

    # Build HTML rows
    cards_html = ""
    for i, c in enumerate(top, 1):
        strength_color = {
            "VERY STRONG": "#16a34a",
            "STRONG": "#2563eb",
            "MODERATE": "#d97706",
            "WEAK": "#6b7280",
        }.get(c["signal_strength"], "#6b7280")

        buyers_rows = "".join(
            f"<tr><td style='padding:4px 8px'>{ins['name']}</td>"
            f"<td style='padding:4px 8px;color:#6b7280'>{ins['role']}</td>"
            f"<td style='padding:4px 8px;text-align:right'>{ins['shares']:,.0f}</td>"
            f"<td style='padding:4px 8px;text-align:right'>${ins['dollars']:,.0f}</td>"
            f"<td style='padding:4px 8px;color:#6b7280'>{ins['date']}</td></tr>"
            for ins in c["insiders"]
        )

        cards_html += f"""
        <div style="background:#fff;border:1px solid #e5e7eb;border-radius:8px;padding:20px;margin-bottom:20px;">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
            <div>
              <span style="font-size:22px;font-weight:700;color:#111">{c['ticker']}</span>
              <span style="margin-left:8px;color:#6b7280;font-size:14px">{c['company']}</span>
            </div>
            <span style="background:{strength_color};color:#fff;padding:4px 10px;border-radius:20px;font-size:12px;font-weight:600">{c['signal_strength']}</span>
          </div>
          <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:16px;">
            <div style="background:#f9fafb;border-radius:6px;padding:10px;text-align:center">
              <div style="font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:.05em">Insiders</div>
              <div style="font-size:24px;font-weight:700;color:#111">{c['insider_count']}</div>
            </div>
            <div style="background:#f9fafb;border-radius:6px;padding:10px;text-align:center">
              <div style="font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:.05em">Total Shares</div>
              <div style="font-size:24px;font-weight:700;color:#111">{c['total_shares']:,.0f}</div>
            </div>
            <div style="background:#f9fafb;border-radius:6px;padding:10px;text-align:center">
              <div style="font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:.05em">Total Value</div>
              <div style="font-size:24px;font-weight:700;color:#2563eb">${c['total_dollars']:,.0f}</div>
            </div>
          </div>
          <table style="width:100%;border-collapse:collapse;font-size:13px;">
            <thead>
              <tr style="border-bottom:2px solid #e5e7eb">
                <th style="padding:4px 8px;text-align:left">Buyer</th>
                <th style="padding:4px 8px;text-align:left">Role</th>
                <th style="padding:4px 8px;text-align:right">Shares</th>
                <th style="padding:4px 8px;text-align:right">Value</th>
                <th style="padding:4px 8px;text-align:left">Date</th>
              </tr>
            </thead>
            <tbody>{buyers_rows}</tbody>
          </table>
          <div style="margin-top:10px;font-size:12px;color:#9ca3af">Period: {c['date_range']}</div>
        </div>"""

    html_body = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f3f4f6;margin:0;padding:20px;">
  <div style="max-width:680px;margin:0 auto;">
    <div style="background:#111827;color:#fff;border-radius:8px;padding:20px 24px;margin-bottom:20px;">
      <h1 style="margin:0;font-size:20px">SEC Insider Cluster Scanner</h1>
      <p style="margin:4px 0 0;color:#9ca3af;font-size:14px">Daily Digest &mdash; {today}</p>
    </div>
    <p style="color:#374151;margin-bottom:16px">Top {len(top)} insider buying clusters detected in the past {LOOKBACK_DAYS} days:</p>
    {cards_html}
    <p style="font-size:12px;color:#9ca3af;text-align:center;margin-top:20px">
      Data sourced from SEC EDGAR Form 4 filings. Not investment advice.
    </p>
  </div>
</body>
</html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[Insider Scanner] Top Clusters — {today}"
    msg["From"] = smtp_user
    msg["To"] = recipient
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, recipient, msg.as_string())
        print(f"Email sent to {recipient}")
    except Exception as e:
        print(f"Email error: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=== SEC Insider Cluster Buy Scanner ===")
    print(f"Looking back {LOOKBACK_DAYS} days, clustering window {CLUSTER_WINDOW_DAYS} days")
    print()

    # 1. Fetch filings
    accessions = get_recent_form4_accessions(LOOKBACK_DAYS)

    # 2. Parse each filing
    all_transactions = []
    xml_fetched = 0
    xml_failed  = 0
    debug_count = 0

    for i, meta in enumerate(accessions):
        if i % 50 == 0:
            print(f"  Parsing filing {i+1}/{len(accessions)} ...")
        xml_text = fetch_filing_xml(meta["accession_no"])
        if not xml_text:
            xml_failed += 1
            continue
        xml_fetched += 1

        is_debug_sample = debug_count < DEBUG_SAMPLES
        if is_debug_sample:
            debug_count += 1
            print(f"\n{'='*60}")
            print(f"DEBUG SAMPLE {debug_count}: accession={meta['accession_no']}")
            print(f"  entity={meta.get('entity_name','?')}  file_date={meta.get('file_date','?')}")
            print(f"  XML snippet (first 800 chars):")
            print(xml_text[:800])
            print()

        txns = parse_form4(xml_text, meta, debug=is_debug_sample)
        if is_debug_sample:
            print(f"  → parse_form4 returned {len(txns)} record(s)")
            print(f"{'='*60}\n")
        all_transactions.extend(txns)
        time.sleep(0.12)  # SEC rate limit: ~10 req/s

    print(f"\nXML fetch stats: {xml_fetched} succeeded, {xml_failed} failed/not-found")
    print(f"Found {len(all_transactions)} open-market C-suite/director purchases")

    # 3. Cluster
    clusters = cluster_transactions(all_transactions)
    print(f"Found {len(clusters)} clusters with {CLUSTER_MIN_INSIDERS}+ insiders")

    # 4. Save JSON for dashboard
    os.makedirs("data", exist_ok=True)
    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "lookback_days": LOOKBACK_DAYS,
        "cluster_window_days": CLUSTER_WINDOW_DAYS,
        "total_transactions": len(all_transactions),
        "total_clusters": len(clusters),
        "clusters": clusters,
        "all_transactions": all_transactions,
    }
    with open(OUTPUT_JSON, "w") as f:
        json.dump(output, f, indent=2)
    print(f"Saved results → {OUTPUT_JSON}")

    # Also copy to docs/ for GitHub Pages
    os.makedirs("docs", exist_ok=True)
    with open("docs/results.json", "w") as f:
        json.dump(output, f, indent=2)
    print("Copied results → docs/results.json")

    # 5. Send email
    if clusters:
        send_email_digest(clusters)
    else:
        print("No clusters found — skipping email.")

    print("\nDone.")


if __name__ == "__main__":
    main()
