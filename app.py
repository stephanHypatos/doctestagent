# streamlit_app.py
# Run with:  streamlit run streamlit_app.py

import time
import csv
import json
import re
from pathlib import Path
from urllib.parse import urljoin, urlencode

import requests
from bs4 import BeautifulSoup
import streamlit as st
import pandas as pd

st.set_page_config(page_title="Medi-Learn Facharzt-Protokolle Scraper", layout="wide")

BASE_URL = "https://www.medi-learn.de/pruefungsprotokolle/facharztpruefung/"
DETAIL_PREFIX = "detailed.php?ID="
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ML-Protokoll-Scraper/1.0)"
}
DEFAULT_SLEEP = 1.0  # polite delay


@st.cache_data(show_spinner=False)
def get_soup(url: str, headers: dict | None = None) -> BeautifulSoup:
    r = requests.get(url, headers=headers or HEADERS, timeout=30)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


def find_select_value_by_label(soup: BeautifulSoup, select_name: str, text_match: str) -> str | None:
    sel = soup.find("select", attrs={"name": select_name})
    if not sel:
        return None
    wanted = text_match.casefold()
    for opt in sel.find_all("option"):
        visible = (opt.get_text(strip=True) or "").casefold()
        if wanted in visible:
            val = opt.get("value")
            if val:
                return val
    return None


@st.cache_data(show_spinner=False)
def discover_filter_values() -> dict:
    """Fetch the search page and map visible labels to (value, select_name)."""
    soup = get_soup(BASE_URL)
    uni_opts = []
    fach_opts = []

    sel_uni = soup.find("select", attrs={"name": "FppUni"})
    if sel_uni:
        for o in sel_uni.find_all("option"):
            t = o.get_text(strip=True)
            v = o.get("value")
            if v:
                uni_opts.append((t, v))

    sel_fach = soup.find("select", attrs={"name": "FppFach"})
    if sel_fach:
        for o in sel_fach.find_all("option"):
            t = o.get_text(strip=True)
            v = o.get("value")
            if v:
                fach_opts.append((t, v))

    return {"uni": uni_opts, "fach": fach_opts}


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


def extract_detail_links_from_list(soup: BeautifulSoup) -> list[str]:
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if DETAIL_PREFIX in href:
            links.append(href if href.startswith("http") else urljoin(BASE_URL, href))
    # de-duplicate
    seen, uniq = set(), []
    for u in links:
        if u not in seen:
            uniq.append(u)
            seen.add(u)
    return uniq


def has_next_page(soup: BeautifulSoup) -> bool:
    for a in soup.find_all("a", href=True):
        if any(x in a.get_text(strip=True) for x in ["Weiter", "Nächste", "»", "->"]):
            return True
    return False


def parse_detail_page(soup: BeautifulSoup, url: str) -> dict:
    data = {"detail_url": url}

    # likely label/value pairs
    labels = [
        "Ort/Uni", "Fach", "Prüfer", "Atmosphäre", "Dauer", "Note",
        "Datum", "Erstellt", "Prüfungsjahr", "Prüfungsort", "Prüfungsdatum"
    ]
    text_blocks = []
    for el in soup.find_all(["p", "li", "div", "td"]):
        t = el.get_text(" ", strip=True)
        if ":" in t:
            text_blocks.append(t)

    for t in text_blocks:
        parts = [x.strip() for x in re.split(r"\s{2,}", t)]
        for seg in parts:
            if ":" in seg:
                lab, val = seg.split(":", 1)
                lab, val = lab.strip(), val.strip()
                if any(lab.startswith(L) for L in labels):
                    data[lab] = val

    # headings for free text sections
    def scrape_section_by_heading(headings):
        # Search for bold or heading-like elements; then gather sibling text
        candidates = soup.find_all(["h1", "h2", "h3", "strong", "b"])
        for h in candidates:
            ht = h.get_text(" ", strip=True)
            for target in headings:
                if target.casefold() in (ht or "").casefold():
                    # collect next siblings within parent (simple heuristic)
                    parent = h.parent if h.parent else soup
                    texts = []
                    for sib in parent.find_all(recursive=False):
                        if sib is h:
                            continue
                        # stop when another heading-like appears
                        if sib.find(["strong", "b"]) or sib.name in ["h1", "h2", "h3"]:
                            break
                        texts.append(sib.get_text(" ", strip=True))
                    out = "\n".join([t for t in texts if t])
                    if out:
                        return out
        return None

    fragen = scrape_section_by_heading(["Fragen", "Themen", "Themen/Fragen"])
    tipps = scrape_section_by_heading(["Tipps", "Hinweise", "Empfehlungen"])
    if fragen:
        data["Fragen_Themen"] = fragen
    if tipps:
        data["Tipps"] = tipps

    title = soup.find("title")
    if title:
        data["title"] = title.get_text(strip=True)

    return data


def scrape(uni_label: str, fach_label: str, max_pages: int, delay_sec: float) -> list[dict]:
    search_soup = get_soup(BASE_URL)
    time.sleep(delay_sec)

    uni_val = find_select_value_by_label(search_soup, "FppUni", uni_label) or ""
    fach_val = find_select_value_by_label(search_soup, "FppFach", fach_label) or ""

    if not uni_val:
        raise RuntimeError(f"Could not resolve Uni option value for '{uni_label}'.")
    if not fach_val:
        raise RuntimeError(f"Could not resolve Fach option value for '{fach_label}'.")

    all_records = []
    page = 1
    with st.spinner("Scraping list pages…"):
        while True:
            list_url = build_results_url(uni_val, fach_val, page_nr=page, page_len=40)
            st.write(f"**Fetching list page {page}:** {list_url}")
            list_soup = get_soup(list_url)
            time.sleep(delay_sec)

            detail_links = extract_detail_links_from_list(list_soup)
            st.write(f"Found {len(detail_links)} detail links on page {page}.")
            for durl in detail_links:
                st.write(f"↳ Visiting detail: {durl}")
                d_soup = get_soup(durl)
                time.sleep(delay_sec)
                rec = parse_detail_page(d_soup, durl)
                all_records.append(rec)

            if page >= max_pages:
                break
            if not has_next_page(list_soup):
                break
            page += 1

    return all_records


# ---- UI ----
st.title(
