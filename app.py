import time, csv, json, re
from pathlib import Path
from urllib.parse import urljoin, urlencode

import requests
from bs4 import BeautifulSoup
import pandas as pd
import streamlit as st  # <-- must be before any @st.cache_* usage

st.set_page_config(page_title="Medi-Learn Scraper", layout="wide")



# --- strengthen HTTP defaults ---
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
    "Connection": "keep-alive",
}

@st.cache_data(show_spinner=False)
def get_soup(url: str, headers: dict | None = None) -> BeautifulSoup:
    r = requests.get(url, headers=headers or HEADERS, timeout=30)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")

def build_results_url(uni_val: str, fach_val: str, page_nr: int = 1, page_len: int = 40) -> str:
    params = {
        "FppStatus": "1",
        "FppPruefer": "0",
        "FppSeitenlaenge": str(page_len),
        "FppSeiteNr": str(page_nr),
        "FppOpdreBy": "erstellt DESC",
        "FppOrderBy": "erstellt DESC",
        "auswahlstarten": "auswahlstarten",
        "FppUni": uni_val,
        "FppFach": fach_val,
    }
    return f"{BASE_URL}?{urlencode(params)}"

def _fetch_listing_html(list_url: str) -> str:
    """
    Fetch the visible list and, if it contains an embedded results frame (FacharztProtokolle.php),
    fetch that frame too. Return the HTML string that actually holds the links.
    """
    # Top-level list page
    r = requests.get(list_url, headers={**HEADERS, "Referer": BASE_URL}, timeout=30)
    r.raise_for_status()
    html = r.text
    soup = BeautifulSoup(html, "html.parser")

    # Look for iframe or embedded PHP that carries the table
    iframe = soup.find("iframe")
    if iframe and iframe.get("src"):
        src = iframe["src"]
        # make absolute
        inner_url = urljoin(BASE_URL, src)
        r2 = requests.get(inner_url, headers={**HEADERS, "Referer": list_url}, timeout=30)
        if r2.ok and ("detailed.php" in r2.text or "FacharztProtokolle.php" in inner_url):
            return r2.text

    # If no iframe, the table may be injected server-side. Return the whole HTML.
    return html

def extract_detail_links_from_listing_html(html: str) -> list[str]:
    """
    Be robust: find anchors, onclicks, or plain text containing detailed.php?ID=…
    """
    links = []

    # 1) normal <a href="detailed.php?ID=...">
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "detailed.php?ID=" in href:
            links.append(href if href.startswith("http") else urljoin(BASE_URL, href))

    # 2) onclick or text snippets -> regex
    rx = re.compile(r"detailed\.php\?ID=\d+")
    for m in rx.findall(html):
        url_abs = urljoin(BASE_URL, m)
        links.append(url_abs)

    # de-duplicate, keep order
    seen, uniq = set(), []
    for u in links:
        if u not in seen:
            uniq.append(u)
            seen.add(u)
    return uniq

def has_next_page_from_html(html: str) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        if any(x in a.get_text(strip=True) for x in ["Weiter", "Nächste", "»", "->"]):
            return True
    return False

def scrape(uni_label: str, fach_label: str, max_pages: int, delay_sec: float) -> list[dict]:
    search_soup = get_soup(BASE_URL)
    time.sleep(delay_sec)

    uni_val = find_select_value_by_label(search_soup, "FppUni", uni_label) or ""
    fach_val = find_select_value_by_label(search_soup, "FppFach", fach_label) or ""
    if not uni_val:
        raise RuntimeError(f"Could not resolve Uni option value for '{uni_label}'.")
    if not fach_val:
        raise RuntimeError(f"Could not resolve Fach option value for '{fach_label}'.")

    all_records, page = [], 1
    with st.spinner("Scraping list pages…"):
        while True:
            list_url = build_results_url(uni_val, fach_val, page_nr=page, page_len=40)
            st.write(f"**Fetching list page {page}:** {list_url}")

            html = _fetch_listing_html(list_url)
            time.sleep(delay_sec)

            detail_links = extract_detail_links_from_listing_html(html)
            st.write(f"Found {len(detail_links)} detail links on page {page}.")

            for durl in detail_links:
                st.write(f"↳ Visiting detail: {durl}")
                d = requests.get(durl, headers={**HEADERS, "Referer": list_url}, timeout=30)
                d.raise_for_status()
                rec = parse_detail_page(BeautifulSoup(d.text, "html.parser"), durl)
                all_records.append(rec)
                time.sleep(delay_sec)

            if page >= max_pages:
                break
            if not has_next_page_from_html(html):
                break
            page += 1

    return all_records
