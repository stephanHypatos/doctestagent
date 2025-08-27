from __future__ import annotations

import re
import time
import urllib.parse
from typing import Dict, List, Optional, Tuple

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup

# ---------- Constants ----------
BASE_URL = "https://www.medi-learn.de"
LIST_PATH = "/pruefungsprotokolle/facharztpruefung/"
LIST_URL = urllib.parse.urljoin(BASE_URL, LIST_PATH)
DETAIL_RE = re.compile(r"detailed\.php\?[^#\s]*id=(\d+)", re.I)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA}

# ---------- Session ----------
def new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    # calm down cookie overlays
    s.cookies.set("CookieConsent", "true", domain="www.medi-learn.de")
    s.cookies.set("CookieConsentBulkSetting-", "1", domain="www.medi-learn.de")
    return s

# ---------- HTML ----------
def get_soup(session: requests.Session, url: str) -> BeautifulSoup:
    r = session.get(url, timeout=30, allow_redirects=True)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")

def find_select(soup: BeautifulSoup, name_or_id: str) -> Optional[BeautifulSoup]:
    el = soup.find("select", id=name_or_id)
    if el:
        return el
    return soup.find("select", attrs={"name": name_or_id})

def select_value_by_visible(select_el: BeautifulSoup, wanted_text: str) -> Tuple[str, str]:
    """
    Choose the <option> by visible text (exact/contains; case-insensitive).
    Return (field_name, option_value). Falls back to common Dresden/Innere values.
    """
    name = select_el.get("name") or select_el.get("id")
    if not name:
        raise RuntimeError("Select has no name/id.")
    wanted = (wanted_text or "").strip().lower()

    # exact
    for opt in select_el.find_all("option"):
        vis = (opt.get_text(strip=True) or "").strip()
        if vis and vis.lower() == wanted:
            return name, (opt.get("value") or vis)

    # contains
    for opt in select_el.find_all("option"):
        vis = (opt.get_text(strip=True) or "").strip()
        if vis and wanted in vis.lower():
            return name, (opt.get("value") or vis)

    # sensible defaults for Dresden / Innere Medizin (from your snippet)
    if wanted == "dresden":
        return name, "5"
    if wanted in ("innere", "innere medizin"):
        return name, "20"

    # fallback: first non-empty
    for opt in select_el.find_all("option"):
        vis = (opt.get_text(strip=True) or "").strip()
        if vis:
            return name, (opt.get("value") or vis)

    return name, wanted_text

# ---------- Build URL like the JS does ----------
def build_results_url(
    start_soup: BeautifulSoup, uni_visible: str, fach_visible: str, rows_per_page: int
) -> str:
    # Find selects right on the page (your snippet)
    uni_sel = find_select(start_soup, "FppUni")
    fach_sel = find_select(start_soup, "FppFach")
    if not uni_sel or not fach_sel:
        raise RuntimeError("FppUni or FppFach select not found on start page.")

    uni_name, uni_val = select_value_by_visible(uni_sel, uni_visible)
    fach_name, fach_val = select_value_by_visible(fach_sel, fach_visible)

    # The JS submit just composes a GET; we do the same:
    params = {
        "FppStatus": "1",
        uni_name: str(uni_val),
        fach_name: str(fach_val),
        "FppSeitenlaenge": str(rows_per_page),
        "auswahlstarten": "auswahlstarten",
    }
    return f"{LIST_URL}?{urllib.parse.urlencode(params)}"

# ---------- Results parsing (container + tbody) ----------
def get_results_table(soup: BeautifulSoup) -> Optional[BeautifulSoup]:
    cont = soup.find("div", id="FacharztpruefungsprotokollContainer")
    if not cont:
        return None
    table = cont.find("table", class_=re.compile(r"\bdiensttabelle\b", re.I))
    return table

def extract_rows_from_table(table: BeautifulSoup) -> List[Dict[str, str]]:
    """
    Exact row shape from your snippet; we parse the <tbody> specifically.
    """
    out: List[Dict[str, str]] = []
    tbody = table.find("tbody") or table  # tolerate missing <tbody> in markup
    for tr in tbody.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 5:
            continue
        ort = tds[0].get("title") or tds[0].get_text(" ", strip=True)
        fach = tds[1].get("title") or tds[1].get_text(" ", strip=True)
        pruefer = tds[2].get("title") or tds[2].get_text(" ", strip=True)
        datum = tds[3].get("title") or tds[3].get_text(" ", strip=True)
        a = tds[4].find("a", href=True)
        if not a:
            continue
        m = DETAIL_RE.search(a["href"])
        if not m:
            continue
        ml_id = m.group(1)
        url = urllib.parse.urljoin(LIST_URL, a["href"])
        out.append(
            {
                "ml_id": ml_id,
                "ort_uni": (ort or "").strip(),
                "fachrichtung": (fach or "").strip(),
                "pruefer": (pruefer or "").strip(),
                "eingefuegt": (datum or "").strip(),
                "url": url,
                "title": a.get("title") or a.get_text(" ", strip=True) or f"Protokoll {ml_id}",
            }
        )
    # de-dup
    uniq, seen = [], set()
    for r in out:
        if r["ml_id"] in seen:
            continue
        seen.add(r["ml_id"])
        uniq.append(r)
    return uniq

def find_next_url_in_container(soup: BeautifulSoup) -> Optional[str]:
    """
    Follow pagination controls rendered near/inside the container.
    """
    cont = soup.find("div", id="FacharztpruefungsprotokollContainer") or soup
    for a in cont.find_all("a", href=True):
        txt = a.get_text(" ", strip=True).lower()
        if any(t in txt for t in ["weiter", "nächste", "next", "»", "›", ">"]):
            full = urllib.parse.urljoin(LIST_URL, a["href"])
            if LIST_PATH in full:
                return full
    return None

# ---------- Details ----------
def fetch_detail_text(session: requests.Session, url: str) -> str:
    r = session.get(url, timeout=30, allow_redirects=True)
    if r.status_code != 200:
        return ""
    return BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True)

# ---------- Workflow ----------
def fetch_first_results_page(
    session: requests.Session, uni_visible: str, fach_visible: str, rows_per_page: int
) -> BeautifulSoup:
    """
    1) GET start page, map visible texts to option values
    2) Build GET URL with params (no POST)
    3) GET the results page and return soup
    """
    start = get_soup(session, LIST_URL)
    results_url = build_results_url(start, uni_visible, fach_visible, rows_per_page)

    r = session.get(results_url, timeout=30, allow_redirects=True)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    # Hard-check: container + table.diensttabelle must exist
    table = get_results_table(soup)
    if not table:
        raise RuntimeError("Results table not found (container/table missing).")
    return soup

def crawl_all_results(
    session: requests.Session,
    first_results_soup: BeautifulSoup,
    max_pages: int,
    pause_pages: float,
) -> List[Dict[str, str]]:
    collected: Dict[str, Dict[str, str]] = {}
    pages = 1

    table = get_results_table(first_results_soup)
    if table:
        for r in extract_rows_from_table(table):
            collected[r["ml_id"]] = r

    next_url = find_next_url_in_container(first_results_soup)
    while next_url and pages < max_pages:
        pages += 1
        if pause_pages:
            time.sleep(pause_pages)
        soup = get_soup(session, next_url)
        table = get_results_table(soup)
        if not table:
            break
        for r in extract_rows_from_table(table):
            collected[r["ml_id"]] = r
        next_url = find_next_url_in_container(soup)

    rows = list(collected.values())
    rows.sort(key=lambda x: int(x["ml_id"]))
    return rows

def enrich_with_detail_texts(
    session: requests.Session, rows: List[Dict[str, str]], pause_details: float
) -> pd.DataFrame:
    out = []
    for r in rows:
        txt = fetch_detail_text(session, r["url"])
        rec = dict(r)
        rec["detail_text"] = txt
        out.append(rec)
        if pause_details:
            time.sleep(pause_details)

    df = pd.DataFrame(out)
    cols = ["ml_id", "ort_uni", "fachrichtung", "pruefer", "eingefuegt", "url", "title", "detail_text"]
    return df[cols]

# ---------- Streamlit UI ----------
st.set_page_config(page_title="Medi-Learn Protokolle Counter", page_icon="🩺", layout="wide")
st.title("🩺 Medi-Learn Facharztprüfungsprotokolle — zählen & Details scrapen")

with st.sidebar:
    st.header("Filter (sichtbarer Text)")
    uni_visible = st.text_input("Uni (FppUni)", value="Dresden")
    fach_visible = st.text_input("Fach (FppFach)", value="Innere Medizin")

    st.header("Ergebnisse")
    rows_per_page = st.selectbox("Anzahl pro Seite (FppSeitenlaenge)", [5,10,15,20,25,30,35,40], index=7)

    st.header("Crawling")
    max_pages = st.slider("Max. Seiten", 1, 80, 20)
    pause_pages = st.slider("Pause zw. Seiten (s)", 0.0, 2.0, 0.2, 0.1)
    pause_details = st.slider("Pause zw. Detail-Seiten (s)", 0.0, 1.0, 0.05, 0.05)

    debug = st.checkbox("Debug: zeige erste 1500 Zeichen der Seite", value=False)
    go = st.button("🔎 Start")

if go:
    try:
        sess = new_session()

        with st.spinner("Parameter-URL bauen & erste Ergebnisse laden…"):
            first_soup = fetch_first_results_page(
                sess, uni_visible.strip(), fach_visible.strip(), rows_per_page
            )
            if debug:
                st.code(str(first_soup)[:1500], language="html")

        with st.spinner("Alle Ergebnis-Seiten sammeln…"):
            rows = crawl_all_results(sess, first_soup, max_pages=max_pages, pause_pages=pause_pages)

        st.subheader("Ergebnisse")
        st.metric("Anzahl Protokolle", len(rows))

        if not rows:
            st.info("Keine Ergebnisse gefunden – prüfe Schreibweise (exakt sichtbarer Text) oder erhöhe Seitenlimit.")
        else:
            with st.spinner("Detailseiten laden & Text erfassen…"):
                df = enrich_with_detail_texts(sess, rows, pause_details=pause_details)

            st.dataframe(df, use_container_width=True)
            csv = df.to_csv(index=False).encode("utf-8")
            st.download_button(
                "⬇️ CSV herunterladen",
                data=csv,
                file_name=f"medi_learn_{uni_visible}_{fach_visible}.csv",
                mime="text/csv",
            )

        st.divider()
        st.markdown(
            "- **Step 1:** Sichtbare Texte in `FppUni`/`FppFach` → Option-Werte ermitteln.\n"
            "- **Step 2:** **GET** auf die Seite mit Parametern (wie `AusfuellenFpp()`), "
            "Ergebnisse in `#FacharztpruefungsprotokollContainer > .diensttabelle > tbody` auslesen.\n"
            "- **Step 3:** Alle Seiten paginieren und **Detailtexte** laden."
        )

    except Exception as exc:
        st.error(f"Fehler: {exc}")
