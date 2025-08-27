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
BROWSER_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": LIST_URL,
}
XHR_HEADERS = {**BROWSER_HEADERS, "X-Requested-With": "XMLHttpRequest"}

DETAIL_RE = re.compile(r"detailed\.php\?[^#\s]*id=(\d+)", re.I)
NEXT_TEXTS = ("weiter", "nächste", "next", "»", "›", ">")

# ----------------- Session helpers -----------------
def new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(BROWSER_HEADERS)
    # calm cookie overlay
    s.cookies.set("CookieConsent", "true", domain="www.medi-learn.de")
    s.cookies.set("CookieConsentBulkSetting-", "1", domain="www.medi-learn.de")
    return s

def to_soup(resp: requests.Response) -> BeautifulSoup:
    try:
        text = resp.content.decode(resp.encoding or "latin-1", errors="ignore")
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

def read_option_values(sess: requests.Session, uni_visible: str, fach_visible: str) -> Tuple[Dict[str, str], BeautifulSoup]:
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
    return {name_uni: str(val_uni), name_fach: str(val_fach)}, soup

def make_params(sel_vals: Dict[str, str], rows_per_page: int, page_nr: int = 1) -> Dict[str, str]:
    return {
        "FppStatus": "1",
        **sel_vals,
        "FppSeitenlaenge": str(rows_per_page),
        "FppSeiteNr": str(page_nr),
        "FppOrderBy": "erstellt DESC",
        "auswahlstarten": "auswahlstarten",
    }

# ----------------- Endpoint discovery (AJAX) -----------------
LOAD_URL_PAT = re.compile(
    r"""(?:
            \.load\(\s*["'](?P<url1>[^"']+)["'] |
            url\s*:\s*["'](?P<url2>[^"']+)["']
        )""",
    re.I | re.X,
)

def discover_ajax_endpoint(start_soup: BeautifulSoup, sess: requests.Session) -> Optional[str]:
    # Find .js files included on the page and scan for .load("...") or url: "..."
    for script in start_soup.find_all("script", src=True):
        src = urllib.parse.urljoin(LIST_URL, script["src"])
        try:
            r = sess.get(src, timeout=20)
            if r.status_code != 200:
                continue
            js = r.text
        except Exception:
            continue
        m = LOAD_URL_PAT.search(js)
        if m:
            url = m.group("url1") or m.group("url2")
            if url:
                return urllib.parse.urljoin(LIST_URL, url)
    return None

# ----------------- Results parsing -----------------
def get_results_table(soup: BeautifulSoup) -> Optional[BeautifulSoup]:
    cont = soup.find("div", id="FacharztpruefungsprotokollContainer")
    if cont:
        tbl = cont.find("table", class_=re.compile(r"\bdiensttabelle\b", re.I))
        if tbl:
            return tbl
    return soup.find("table", class_=re.compile(r"\bdiensttabelle\b", re.I))

def extract_rows(table: BeautifulSoup) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    tbody = table.find("tbody") or table
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
        rows.append(
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
    for r in rows:
        if r["ml_id"] in seen:
            continue
        seen.add(r["ml_id"])
        uniq.append(r)
    return uniq

def find_next_url_in_container(soup: BeautifulSoup) -> Optional[str]:
    cont = soup.find("div", id="FacharztpruefungsprotokollContainer") or soup
    for a in cont.find_all("a", href=True):
        txt = a.get_text(" ", strip=True).lower()
        if any(t in txt for t in NEXT_TEXTS):
            full = urllib.parse.urljoin(LIST_URL, a["href"])
            if LIST_PATH in full:
                return full
    return None

# ----------------- Fetch results (multi-strategy) -----------------
def fetch_results_page(sess: requests.Session, params: Dict[str, str], start_soup: BeautifulSoup,
                       debug: bool = False) -> BeautifulSoup:
    # Strategy 1: simple GET to main URL with params (some setups return full page)
    url = f"{LIST_URL}?{urllib.parse.urlencode(params)}"
    r = sess.get(url, timeout=30, allow_redirects=True)
    soup = to_soup(r)
    if get_results_table(soup):
        return soup

    # Strategy 2: discover AJAX endpoint from included JS and call with XHR headers
    endpoint = discover_ajax_endpoint(start_soup, sess)
    if endpoint:
        r2 = sess.get(endpoint, params=params, headers=XHR_HEADERS, timeout=30)
        s2 = to_soup(r2)
        # if it's a fragment, wrap into a container so parser finds the table
        if get_results_table(s2):
            return s2
        # sometimes endpoint returns just the container; ensure we still pass it on
        cont = s2.find("div", id="FacharztpruefungsprotokollContainer") or s2.find(
            "table", class_=re.compile(r"\bdiensttabelle\b", re.I)
        )
        if cont:
            wrapper = BeautifulSoup("<div id='FacharztpruefungsprotokollContainer'></div>", "html.parser")
            wrapper.div.append(cont)
            return wrapper

    # Strategy 3: try common guesses (rarely needed)
    guesses = [
        urllib.parse.urljoin(LIST_URL, "FacharztProtokolle.php"),
        urllib.parse.urljoin(LIST_URL, "facharztprotokolle.php"),
        urllib.parse.urljoin(LIST_URL, "ajax.php"),
    ]
    for g in guesses:
        r3 = sess.get(g, params=params, headers=XHR_HEADERS, timeout=30)
        s3 = to_soup(r3)
        if get_results_table(s3):
            return s3

    # If we got here, surface a helpful error
    snippet = str(soup)[:1200]
    if debug:
        st.code(snippet, language="html")
    raise RuntimeError("Results table not found (AJAX endpoint likely different).")

# ----------------- Details -----------------
def fetch_detail_text(sess: requests.Session, url: str) -> str:
    r = sess.get(url, timeout=30, allow_redirects=True)
    if r.status_code != 200:
        return ""
    try:
        text = r.content.decode(r.encoding or "latin-1", errors="ignore")
    except Exception:
        text = r.content.decode("latin-1", errors="ignore")
    return BeautifulSoup(text, "html.parser").get_text(" ", strip=True)

# ----------------- Workflow -----------------
def crawl_all(sess: requests.Session, uni_visible: str, fach_visible: str, rows_per_page: int,
              max_pages: int, pause_pages: float, debug: bool=False) -> List[Dict[str, str]]:
    sel_vals, start_soup = read_option_values(sess, uni_visible, fach_visible)
    params = make_params(sel_vals, rows_per_page, page_nr=1)
    first_soup = fetch_results_page(sess, params, start_soup, debug=debug)

    collected: Dict[str, Dict[str, str]] = {}
    pages = 1
    table = get_results_table(first_soup)
    if table:
        for r in extract_rows(table):
            collected[r["ml_id"]] = r

    next_url = find_next_url_in_container(first_soup)
    while next_url and pages < max_pages:
        pages += 1
        if pause_pages:
            time.sleep(pause_pages)
        r = sess.get(next_url, timeout=30)
        s = to_soup(r)
        t = get_results_table(s)
        if not t:
            break
        for r_ in extract_rows(t):
            collected[r_["ml_id"]] = r_
        next_url = find_next_url_in_container(s)

    out = list(collected.values())
    out.sort(key=lambda x: int(x["ml_id"]))
    return out

def enrich_with_details(sess: requests.Session, rows: List[Dict[str, str]], pause_details: float) -> pd.DataFrame:
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
st.title("🩺 Medi-Learn Facharztprüfungsprotokolle — zählen & Details (AJAX-aware)")

with st.sidebar:
    st.header("Filter (sichtbarer Text)")
    uni_visible = st.text_input("Uni (FppUni)", value="Dresden")
    fach_visible = st.text_input("Fach (FppFach)", value="Innere Medizin")

    st.header("Ergebnisse")
    rows_per_page = st.selectbox("Anzahl pro Seite", [5, 10, 15, 20, 25, 30, 35, 40], index=7)
    max_pages = st.slider("Max. Seiten", 1, 80, 20)
    pause_pages = st.slider("Pause zw. Seiten (s)", 0.0, 2.0, 0.2, 0.1)

    st.header("Details")
    load_details = st.checkbox("Detailseiten-Text mitladen", value=True)
    pause_details = st.slider("Pause zw. Detail-Seiten (s)", 0.0, 1.0, 0.05, 0.05)

    debug = st.checkbox("Debug: zeige HTML-Snippets, wenn Tabelle fehlt", value=False)
    go = st.button("🔎 Start")

if go:
    try:
        sess = new_session()
        rows = crawl_all(
            sess,
            uni_visible.strip(),
            fach_visible.strip(),
            int(rows_per_page),
            int(max_pages),
            float(pause_pages),
            debug=debug,
        )

        st.subheader("Ergebnisse")
        st.metric("Anzahl Protokolle", len(rows))

        if not rows:
            st.info("Keine Ergebnisse – prüfe Schreibweise (exakt sichtbarer Text) oder erhöhe die Seitenzahl.")
        else:
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
