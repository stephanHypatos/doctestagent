from __future__ import annotations

import re
import time
import urllib.parse
from typing import Dict, List, Optional

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup

BASE_URL = "https://www.medi-learn.de"
LIST_URL = "https://www.medi-learn.de/pruefungsprotokolle/facharztpruefung/"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA}

DETAIL_RE = re.compile(r"detailed\.php\?ID=(\d+)", re.I)

# Robust regexes for field extraction on detail pages
RE_UNI_LINE = re.compile(r"(?:Ort/Uni|Ort|Universit[aä]t)\s*:\s*(.+?)\s*(?:Fach|Prüfer|Atmosph|Dauer|Note|$)", re.I | re.S)
RE_FACH_LINE = re.compile(r"\bFach\s*:\s*(.+?)\s*(?:Prüfer|Atmosph|Dauer|Note|$)", re.I | re.S)


def absolute_url(base: str, href: Optional[str]) -> Optional[str]:
    if not href:
        return None
    return urllib.parse.urljoin(base, href)


def get_soup(session: requests.Session, url: str) -> BeautifulSoup:
    r = session.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


def extract_listing_detail_links(soup: BeautifulSoup) -> List[Dict[str, str]]:
    """Find all links to detailed.php?ID=… on a listing page."""
    rows: List[Dict[str, str]] = []
    for a in soup.find_all("a", href=True):
        m = DETAIL_RE.search(a["href"])
        if not m:
            continue
        ml_id = m.group(1)
        url = absolute_url(LIST_URL, a["href"])
        title = a.get_text(" ", strip=True) or f"Protokoll {ml_id}"
        if url:
            rows.append({"ml_id": ml_id, "title": title, "url": url})
    # de-dup
    seen = set()
    uniq = []
    for r in rows:
        if r["ml_id"] in seen:
            continue
        seen.add(r["ml_id"])
        uniq.append(r)
    return uniq


def find_next_listing_url(soup: BeautifulSoup) -> Optional[str]:
    """Find a 'next' link on listing pages (Weiter / Nächste / Next / » / ›)."""
    for a in soup.find_all("a", href=True):
        txt = a.get_text(" ", strip=True).lower()
        if any(t in txt for t in ["weiter", "nächste", "next", "»", "›", ">"]):
            full = absolute_url(LIST_URL, a["href"])
            if full and "/pruefungsprotokolle/facharztpruefung" in full:
                return full
    return None


def fetch_detail_info(session: requests.Session, url: str) -> Dict[str, str]:
    """Load a detail page and extract Ort/Uni and Fach text snippets."""
    try:
        r = session.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        html = r.text
    except Exception:
        return {"uni": "", "fach": ""}

    # Quick text version (faster than soup.get_text on big pages)
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)

    uni = ""
    fach = ""
    m_uni = RE_UNI_LINE.search(text)
    if m_uni:
        uni = re.sub(r"\s+", " ", m_uni.group(1)).strip()

    m_fach = RE_FACH_LINE.search(text)
    if m_fach:
        fach = re.sub(r"\s+", " ", m_fach.group(1)).strip()

    return {"uni": uni, "fach": fach}


def crawl_and_filter(
    uni_contains: str,
    fach_contains: str,
    max_listing_pages: int,
    pause_between_pages: float,
    pause_between_details: float,
) -> pd.DataFrame:
    """
    Crawl listing pages and detail pages; keep only those where the extracted
    Uni and Fach fields contain the given substrings (case-insensitive).
    """
    sess = requests.Session()
    sess.headers.update(HEADERS)

    # 1) Crawl listing pages to collect detail links
    collected: Dict[str, Dict[str, str]] = {}
    next_url: Optional[str] = LIST_URL
    pages_crawled = 0

    while next_url and pages_crawled < max_listing_pages:
        pages_crawled += 1
        soup = get_soup(sess, next_url)
        for row in extract_listing_detail_links(soup):
            collected[row["ml_id"]] = row
        next_url = find_next_listing_url(soup)
        if next_url:
            time.sleep(pause_between_pages)

    # 2) Visit each detail and filter
    want_uni = uni_contains.lower().strip()
    want_fach = fach_contains.lower().strip()

    rows = []
    for i, row in enumerate(collected.values(), start=1):
        info = fetch_detail_info(sess, row["url"])
        uni_text = info["uni"].lower()
        fach_text = info["fach"].lower()

        if want_uni in uni_text and want_fach in fach_text:
            rows.append(
                {
                    "ml_id": row["ml_id"],
                    "title": row["title"],
                    "url": row["url"],
                    "uni": info["uni"],
                    "fach": info["fach"],
                }
            )

        if pause_between_details > 0:
            time.sleep(pause_between_details)

    df = pd.DataFrame(rows).sort_values("ml_id", key=lambda s: s.astype(int))
    return df.reset_index(drop=True)


# ---------------- UI ----------------

st.set_page_config(page_title="Medi-Learn Protokolle Counter", page_icon="🩺", layout="wide")
st.title("🩺 Medi-Learn Facharzt-Prüfungsprotokolle — einfacher Crawler")
st.caption(
    "Durchsucht die Listen-Seiten, öffnet jede Detailseite und filtert nach Ort/Uni & Fach. "
    "Keine Formulare, keine JavaScript-Abhängigkeiten."
)

with st.sidebar:
    st.header("Filter")
    uni = st.text_input("Ort/Uni enthält …", value="Dresden")
    fach = st.text_input("Fach enthält …", value="Innere")

    st.header("Crawl-Einstellungen")
    max_pages = st.slider("Max. Listing-Seiten", 1, 60, 20)
    pause_pages = st.slider("Pause zwischen Listing-Seiten (s)", 0.0, 2.0, 0.2, 0.1)
    pause_details = st.slider("Pause zwischen Detail-Seiten (s)", 0.0, 1.0, 0.05, 0.05)

    go = st.button("🔎 Start")

if go:
    try:
        with st.spinner("Crawle Listing-Seiten und prüfe Details…"):
            df = crawl_and_filter(
                uni_contains=uni,
                fach_contains=fach,
                max_listing_pages=max_pages,
                pause_between_pages=pause_pages,
                pause_between_details=pause_details,
            )

        st.subheader("Ergebnis")
        st.metric("Anzahl passender Protokolle", len(df))

        if len(df):
            st.dataframe(df, use_container_width=True)
            csv = df.to_csv(index=False).encode("utf-8")
            st.download_button(
                "⬇️ CSV herunterladen",
                data=csv,
                file_name=f"medi_learn_dresden_innere_{len(df)}.csv",
                mime="text/csv",
            )
        else:
            st.info(
                "Keine Treffer. Tipp: Probiere Varianten (z. B. „TU Dresden“, „Innere Medizin“)."
            )

        st.divider()
        st.markdown(
            "- Zählt ausschließlich Detailseiten, deren **Ort/Uni** den Filtertext enthält "
            "und deren **Fach** den Filtertext enthält.\n"
            "- Erhöhen Sie *Max. Listing-Seiten*, um weiter in die Historie zu gehen."
        )
    except Exception as exc:
        st.error(f"Fehler: {exc}")
