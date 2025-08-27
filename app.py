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

# ---------- Utilities ----------
def absolute_url(base: str, href: Optional[str]) -> Optional[str]:
    return urllib.parse.urljoin(base, href) if href else None

def new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    # helps if Cookiebot gates the page
    s.cookies.set("CookieConsent", "true", domain="www.medi-learn.de")
    s.cookies.set("CookieConsentBulkSetting-", "1", domain="www.medi-learn.de")
    return s

def get_soup(session: requests.Session, url: str) -> BeautifulSoup:
    r = session.get(url, timeout=30)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")

def select_value_by_visible(select_el: BeautifulSoup, wanted_text: str) -> Tuple[str, str]:
    """
    Choose the <option> value by visible text (exact/contains, case-insensitive).
    Returns (field_name, option_value).
    """
    name = select_el.get("name") or select_el.get("id")
    if not name:
        raise RuntimeError("Select has no name/id.")
    wanted = (wanted_text or "").strip().lower()

    # 1) exact (normalized)
    for opt in select_el.find_all("option"):
        vis = (opt.get_text(strip=True) or "").strip()
        if vis and vis.lower() == wanted:
            return name, (opt.get("value") or vis)
    # 2) contains
    for opt in select_el.find_all("option"):
        vis = (opt.get_text(strip=True) or "").strip()
        if vis and (wanted in vis.lower()):
            return name, (opt.get("value") or vis)
    # 3) special fallback for common pair
    if wanted == "dresden":
        return name, "5"
    if wanted in ("innere", "innere medizin"):
        return name, "20"
    # 4) fallback: first non-empty
    for opt in select_el.find_all("option"):
        vis = (opt.get_text(strip=True) or "").strip()
        if vis:
            return name, (opt.get("value") or vis)
    return name, wanted_text

def build_payload_from_start(
    start_soup: BeautifulSoup,
    uni_visible: str,
    fach_visible: str,
    rows_per_page: int,
) -> Tuple[str, Dict[str, str]]:
    """
    Locate FppUni and FppFach selects, compute values, build POST payload.
    Returns (action_url, payload).
    """
    # Find selects by id/name from your snippet
    uni_sel = start_soup.find("select", id="FppUni") or start_soup.find("select", attrs={"name": "FppUni"})
    fach_sel = start_soup.find("select", id="FppFach") or start_soup.find("select", attrs={"name": "FppFach"})
    if not uni_sel or not fach_sel:
        raise RuntimeError("FppUni or FppFach select not found on start page.")

    uni_name, uni_val = select_value_by_visible(uni_sel, uni_visible)
    fach_name, fach_val = select_value_by_visible(fach_sel, fach_visible)

    # Find the wrapping form (if present) to capture action + hidden inputs
    wrapper_form = uni_sel.find_parent("form") or fach_sel.find_parent("form")
    action_url = LIST_URL
    payload: Dict[str, str] = {}

    if wrapper_form:
        action = wrapper_form.get("action") or LIST_URL
        action_url = absolute_url(LIST_URL, action) or LIST_URL
        # hidden inputs (incl. CSRF if any)
        for el in wrapper_form.select("input[type=hidden]"):
            n = el.get("name")
            if n:
                payload[n] = el.get("value", "") or ""

    # Required fields seen in your HTML
    payload["FppStatus"] = payload.get("FppStatus", "1")
    payload[uni_name] = str(uni_val)
    payload[fach_name] = str(fach_val)

    # rows per page (id FppSeitenlaenge)
    payload["FppSeitenlaenge"] = str(rows_per_page)

    # Some servers require submit name/value; image button in snippet is "auswahlstarten"
    payload["auswahlstarten"] = payload.get("auswahlstarten", "auswahlstarten")
    return action_url, payload

# ---------- Results parsing ----------
def find_results_table(soup: BeautifulSoup) -> Optional[BeautifulSoup]:
    # Prefer the container then the table
    cont = soup.find("div", id="FacharztpruefungsprotokollContainer")
    if cont:
        tbl = cont.find("table", class_=re.compile(r"\bdiensttabelle\b", re.I))
        if tbl:
            return tbl
    return soup.find("table", class_=re.compile(r"\bdiensttabelle\b", re.I))

def extract_rows_from_results_table(table: BeautifulSoup) -> List[Dict[str, str]]:
    """
    Parse rows like:
    <tr><td title="Dresden">…</td><td title="Innere/…">…</td>
        <td title="Prüfer">…</td><td title="27.07.2025">…</td>
        <td><a href="...detailed.php?ID=8936" class="details">Details</a></td></tr>
    """
    out: List[Dict[str, str]] = []
    for tr in table.find_all("tr"):
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
        url = absolute_url(LIST_URL, a["href"])
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
    # de-dup by ml_id
    uniq, seen = [], set()
    for r in out:
        if r["ml_id"] in seen:
            continue
        seen.add(r["ml_id"])
        uniq.append(r)
    return uniq

def find_next_results_url(soup: BeautifulSoup) -> Optional[str]:
    # common next labels
    for a in soup.find_all("a", href=True):
        txt = a.get_text(" ", strip=True).lower()
        if any(t in txt for t in ["weiter", "nächste", "next", "»", "›", ">"]):
            full = absolute_url(LIST_URL, a["href"])
            if full and LIST_PATH in full:
                return full
    return None

# ---------- Details scraping ----------
def fetch_detail_text(session: requests.Session, url: str) -> str:
    r = session.get(url, timeout=30)
    if r.status_code != 200:
        return ""
    # simplest: take whole visible text; you can refine later with a target container
    txt = BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True)
    return txt

# ---------- Workflow ----------
def submit_and_get_first_results(
    session: requests.Session, uni_visible: str, fach_visible: str, rows_per_page: int
) -> BeautifulSoup:
    start = get_soup(session, LIST_URL)
    action_url, payload = build_payload_from_start(start, uni_visible, fach_visible, rows_per_page)

    # Try POST first
    resp = session.post(action_url, data=payload, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    if find_results_table(soup):
        return soup

    # Fallback: GET with params
    url = action_url
    url += ("&" if "?" in url else "?") + urllib.parse.urlencode(payload)
    resp = session.get(url, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    if not find_results_table(soup):
        raise RuntimeError("Results table not found after POST/GET.")
    return soup

def crawl_all_results(
    session: requests.Session,
    first_results_soup: BeautifulSoup,
    max_pages: int,
    pause_pages: float,
) -> List[Dict[str, str]]:
    collected: Dict[str, Dict[str, str]] = {}
    pages = 1

    tbl = find_results_table(first_results_soup)
    if tbl:
        for r in extract_rows_from_results_table(tbl):
            collected[r["ml_id"]] = r

    next_url = find_next_results_url(first_results_soup)
    while next_url and pages < max_pages:
        pages += 1
        if pause_pages:
            time.sleep(pause_pages)
        soup = get_soup(session, next_url)
        tbl = find_results_table(soup)
        if not tbl:
            break
        for r in extract_rows_from_results_table(tbl):
            collected[r["ml_id"]] = r
        next_url = find_next_results_url(soup)

    # return list sorted by id
    rows = list(collected.values())
    rows.sort(key=lambda x: int(x["ml_id"]))
    return rows

def enrich_with_detail_texts(
    session: requests.Session, rows: List[Dict[str, str]], pause_details: float
) -> pd.DataFrame:
    out = []
    for i, r in enumerate(rows, start=1):
        txt = fetch_detail_text(session, r["url"])
        rec = dict(r)
        rec["detail_text"] = txt
        out.append(rec)
        if pause_details:
            time.sleep(pause_details)
    df = pd.DataFrame(out)
    # nice column order
    cols = [
        "ml_id",
        "ort_uni",
        "fachrichtung",
        "pruefer",
        "eingefuegt",
        "url",
        "title",
        "detail_text",
    ]
    return df[cols]

# ---------- Streamlit UI ----------
st.set_page_config(page_title="Medi-Learn Protokolle Counter", page_icon="🩺", layout="wide")
st.title("🩺 Medi-Learn Facharztprüfungsprotokolle — Zähler & Detail-Scraper")

with st.sidebar:
    st.header("Filter (Form selects)")
    uni_visible = st.text_input("Uni (sichtbarer Text in FppUni)", value="Dresden")
    fach_visible = st.text_input("Fach (sichtbarer Text in FppFach)", value="Innere Medizin")

    st.header("Ergebnis-Seite")
    rows_per_page = st.selectbox("Anzahl pro Seite (FppSeitenlaenge)", [5,10,15,20,25,30,35,40], index=7)

    st.header("Crawling")
    max_pages = st.slider("Max. Seiten", 1, 80, 20)
    pause_pages = st.slider("Pause zw. Seiten (s)", 0.0, 2.0, 0.2, 0.1)

    st.header("Details")
    enrich = st.checkbox("Detailseiten-Text mitladen (für spätere Zusammenfassung/Klassifikation)", value=True)
    pause_details = st.slider("Pause zw. Detail-Seiten (s)", 0.0, 1.0, 0.05, 0.05)

    debug = st.checkbox("Debug-Ausgaben zeigen", value=False)
    go = st.button("🔎 Start")

if go:
    try:
        sess = new_session()

        with st.spinner("Form absenden & erste Ergebnisse laden…"):
            first_soup = submit_and_get_first_results(sess, uni_visible.strip(), fach_visible.strip(), rows_per_page)

        with st.spinner("Alle Ergebnisseiten sammeln…"):
            rows = crawl_all_results(sess, first_soup, max_pages=max_pages, pause_pages=pause_pages)

        st.subheader("Ergebnisse")
        st.metric("Anzahl Protokolle", len(rows))

        if not rows:
            st.info("Keine Ergebnisse gefunden. Prüfe die sichtbaren Texte (z. B. exakt 'Dresden', 'Innere Medizin').")
        else:
            if debug:
                st.write("Beispiel-Zeile:", rows[0] if rows else {})

            if enrich:
                with st.spinner("Detailseiten laden & Text erfassen…"):
                    df = enrich_with_detail_texts(sess, rows, pause_details=pause_details)
            else:
                df = pd.DataFrame(rows)[
                    ["ml_id", "ort_uni", "fachrichtung", "pruefer", "eingefuegt", "url", "title"]
                ]

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
            "- **Schritt 1:** Form-Felder `FppUni` und `FppFach` setzen und absenden.\n"
            "- **Schritt 2:** Alle Zeilen aus `diensttabelle` aller Seiten sammeln und zählen.\n"
            "- **Schritt 3:** (optional) Jede `detailed.php?ID=…` laden und den reinen Text speichern, "
            "damit du ihn später zusammenfassen/klassifizieren kannst."
        )
    except Exception as exc:
        st.error(f"Fehler: {exc}")
