from __future__ import annotations

import re
import time
import urllib.parse
from typing import Dict, List, Optional, Tuple

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup

# ----------------- Constants -----------------
BASE_URL = "https://www.medi-learn.de"
LIST_PATH = "/pruefungsprotokolle/facharztpruefung/"
LIST_URL = urllib.parse.urljoin(BASE_URL, LIST_PATH)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": LIST_URL,
}

# capture detailed links anywhere on the page, id parameter case-insensitive, allow extra params/anchors
DETAIL_RE = re.compile(r"detailed\.php\?[^#\s]*id=(\d+)", re.I)

# ----------------- Session helpers -----------------
def new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    # consent cookies on both apex + www
    for dom in (".medi-learn.de", "www.medi-learn.de"):
        s.cookies.set("CookieConsent", "true", domain=dom)
        s.cookies.set("CookieConsentBulkSetting-", "1", domain=dom)
    return s

def to_soup(resp: requests.Response) -> BeautifulSoup:
    enc = resp.encoding or "utf-8"
    try:
        text = resp.content.decode(enc, errors="ignore")
    except Exception:
        text = resp.content.decode("latin-1", errors="ignore")
    return BeautifulSoup(text, "html.parser")

# ----------------- Form helpers -----------------
def find_select(soup: BeautifulSoup, name_or_id: str) -> Optional[BeautifulSoup]:
    return soup.find("select", id=name_or_id) or soup.find("select", attrs={"name": name_or_id})

def option_value_for_visible(select_el: BeautifulSoup, wanted_visible: str,
                             defaults: dict[str, str] | None = None) -> Tuple[str, str]:
    field_name = select_el.get("name") or select_el.get("id") or ""
    want = (wanted_visible or "").strip().lower()

    for opt in select_el.find_all("option"):
        vis = (opt.get_text(strip=True) or "").strip()
        if vis and vis.lower() == want:
            return field_name, (opt.get("value") or vis)
    for opt in select_el.find_all("option"):
        vis = (opt.get_text(strip=True) or "").strip()
        if vis and want in vis.lower():
            return field_name, (opt.get("value") or vis)
    if defaults and want in defaults:
        return field_name, defaults[want]
    for opt in select_el.find_all("option"):
        vis = (opt.get_text(strip=True) or "").strip()
        if vis:
            return field_name, (opt.get("value") or vis)
    return field_name, wanted_visible

def read_option_values(sess: requests.Session, uni_visible: str, fach_visible: str) -> Dict[str, str]:
    r = sess.get(LIST_URL, timeout=30)
    r.raise_for_status()
    soup = to_soup(r)

    uni_sel = find_select(soup, "FppUni")
    fach_sel = find_select(soup, "FppFach")
    if not uni_sel or not fach_sel:
        raise RuntimeError("FppUni/FppFach selects not found on the page.")

    name_uni, val_uni = option_value_for_visible(uni_sel, uni_visible, {"dresden": "5"})
    name_fach, val_fach = option_value_for_visible(
        fach_sel, fach_visible, {"innere medizin": "20", "innere": "20"}
    )
    return {name_uni: str(val_uni), name_fach: str(val_fach)}

def make_params(sel_vals: Dict[str, str], rows_per_page: int, page_nr: int) -> Dict[str, str]:
    """
    Build GET params like a real click on the image submit would do.
    Includes:
      - FppPruefer=0 (alle)
      - BOTH spellings for order key (misspelled FppOpdreBy and correct FppOrderBy)
      - auswahlstarten + image submit coords
    """
    base = {
        "FppStatus": "1",
        "FppPruefer": "0",
        "FppSeitenlaenge": str(rows_per_page),
        "FppSeiteNr": str(page_nr),
        "FppOpdreBy": "erstellt DESC",   # misspelled (seen in DOM)
        "FppOrderBy": "erstellt DESC",   # also send correct spelling
        "auswahlstarten": "auswahlstarten",
        "auswahlstarten.x": "12",
        "auswahlstarten.y": "9",
    }
    base.update(sel_vals)  # adds FppUni + FppFach
    return base

# ----------------- Results extraction (layout-agnostic) -----------------
def extract_detail_links_anywhere(soup: BeautifulSoup) -> List[Dict[str, str]]:
    """
    Robust: scan the entire page for detailed.php?ID=... links and, if available,
    lift adjacent table-cell info (Ort/Fach/Prüfer/Datum). Works even if the
    container/table markup differs or is missing.
    """
    rows: List[Dict[str, str]] = []
    seen: set[str] = set()

    for a in soup.find_all("a", href=True):
        m = DETAIL_RE.search(a["href"])
        if not m:
            continue
        ml_id = m.group(1)
        if ml_id in seen:
            continue
        seen.add(ml_id)
        url = urllib.parse.urljoin(LIST_URL, a["href"])

        # Try to pull structured cells if the link is inside a row
        ort = fach = pruefer = datum = ""
        tr = a.find_parent("tr")
        if tr:
            tds = tr.find_all("td")
            if len(tds) >= 4:
                ort = tds[0].get("title") or tds[0].get_text(" ", strip=True)
                fach = tds[1].get("title") or tds[1].get_text(" ", strip=True)
                pruefer = tds[2].get("title") or tds[2].get_text(" ", strip=True)
                datum = tds[3].get("title") or tds[3].get_text(" ", strip=True)

        title = a.get("title") or a.get_text(" ", strip=True) or f"Protokoll {ml_id}"
        rows.append(
            {
                "ml_id": ml_id,
                "ort_uni": (ort or "").strip(),
                "fachrichtung": (fach or "").strip(),
                "pruefer": (pruefer or "").strip(),
                "eingefuegt": (datum or "").strip(),
                "url": url,
                "title": title,
            }
        )
    rows.sort(key=lambda x: int(x["ml_id"]))
    return rows

# ----------------- Details -----------------
def fetch_detail_text(sess: requests.Session, url: str) -> str:
    r = sess.get(url, timeout=30, allow_redirects=True)
    if r.status_code != 200:
        return ""
    enc = r.encoding or "utf-8"
    try:
        text = r.content.decode(enc, errors="ignore")
    except Exception:
        text = r.content.decode("latin-1", errors="ignore")
    return BeautifulSoup(text, "html.parser").get_text(" ", strip=True)

# ----------------- Crawl via page number loop -----------------
def crawl_all_pages_by_number(
    sess: requests.Session,
    sel_vals: Dict[str, str],
    rows_per_page: int,
    max_pages: int,
    pause_pages: float,
    debug: bool = False,
) -> List[Dict[str, str]]:
    collected: Dict[str, Dict[str, str]] = {}
    for page_nr in range(1, max_pages + 1):
        params = make_params(sel_vals, rows_per_page, page_nr=page_nr)
        url = f"{LIST_URL}?{urllib.parse.urlencode(params)}"
        r = sess.get(url, timeout=30, allow_redirects=True)
        soup = to_soup(r)

        if debug:
            st.caption(f"GET page {page_nr} → {r.url} (HTTP {r.status_code}, bytes={len(r.content)})")

        # Extract links anywhere (table present or not)
        found = extract_detail_links_anywhere(soup)
        if debug:
            st.caption(f"Page {page_nr}: found {len(found)} detail links")

        # Stop if no links at this page number (assuming no holes)
        if not found:
            break

        for row in found:
            collected[row["ml_id"]] = row

        if pause_pages:
            time.sleep(pause_pages)

    out = list(collected.values())
    out.sort(key=lambda x: int(x["ml_id"]))
    return out

def enrich_with_details(
    sess: requests.Session, rows: List[Dict[str, str]], pause_details: float
) -> pd.DataFrame:
    out = []
    for r in rows:
        txt = fetch_detail_text(sess, r["url"])
        rec = dict(r)
        rec["detail_text"] = txt
        out.append(rec)
        if pause_details:
            time.sleep(pause_details)
    df = pd.DataFrame(out)
    cols = ["ml_id", "ort_uni", "fachrichtung", "pruefer", "eingefuegt", "url", "title", "detail_text"]
    return df[cols]

# ----------------- Streamlit UI -----------------
st.set_page_config(page_title="Medi-Learn Protokolle — zählen & Details", page_icon="🩺", layout="wide")
st.title("🩺 Medi-Learn Facharztprüfungsprotokolle — zählen & Details (robuste GET-Paginierung)")

with st.sidebar:
    st.header("Filter (sichtbarer Text)")
    uni_visible = st.text_input("Uni (FppUni)", value="Dresden")
    fach_visible = st.text_input("Fach (FppFach)", value="Innere Medizin")

    st.header("Ergebnisse")
    rows_per_page = st.selectbox("Anzahl pro Seite", [5, 10, 15, 20, 25, 30, 35, 40], index=7)
    max_pages = st.slider("Max. Seiten", 1, 120, 40)
    pause_pages = st.slider("Pause zw. Seiten (s)", 0.0, 2.0, 0.2, 0.1)

    st.header("Details")
    load_details = st.checkbox("Detailseiten-Text mitladen", value=True)
    pause_details = st.slider("Pause zw. Detail-Seiten (s)", 0.0, 1.0, 0.05, 0.05)

    debug = st.checkbox("Debug: pro Seite Linkanzahl & URL zeigen", value=False)
    go = st.button("🔎 Start")

if go:
    try:
        sess = new_session()
        # 1) Map visible strings to actual option values (e.g., Dresden→5, Innere Medizin→20)
        sel_vals = read_option_values(sess, uni_visible.strip(), fach_visible.strip())

        # 2) Crawl by incrementing FppSeiteNr=1..N, extracting detail links anywhere on page
        rows = crawl_all_pages_by_number(
            sess,
            sel_vals=sel_vals,
            rows_per_page=int(rows_per_page),
            max_pages=int(max_pages),
            pause_pages=float(pause_pages),
            debug=debug,
        )

        st.subheader("Ergebnisse")
        st.metric("Anzahl Protokolle", len(rows))

        if not rows:
            st.info("Keine Ergebnisse – prüfe die sichtbaren Texte oder erhöhe die Seitenzahl.")
        else:
            # 3) Fetch details if requested
            if load_details:
                with st.spinner("Detailseiten laden …"):
                    df = enrich_with_details(sess, rows, float(pause_details))
            else:
                df = pd.DataFrame(rows)[["ml_id", "ort_uni", "fachrichtung", "pruefer", "eingefuegt", "url", "title"]]

            st.dataframe(df, use_container_width=True)
            csv = df.to_csv(index=False).encode("utf-8")
            st.download_button(
                "⬇️ CSV herunterladen",
                data=csv,
                file_name=f"medi_learn_{uni_visible}_{fach_visible}.csv",
                mime="text/csv",
            )

    except Exception as exc:
        st.error(f"Fehler: {exc}")
