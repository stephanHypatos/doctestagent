from __future__ import annotations

import re
import time
import urllib.parse
from typing import Dict, List, Optional, Tuple

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup

BASE_URL = "https://www.medi-learn.de"
LIST_PATH = "/pruefungsprotokolle/facharztpruefung/"
LIST_URL = urllib.parse.urljoin(BASE_URL, LIST_PATH)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA}

DETAIL_RE = re.compile(r"detailed\.php\?[^#\s]*id=(\d+)", re.I)

def absolute_url(base: str, href: Optional[str]) -> Optional[str]:
    return urllib.parse.urljoin(base, href) if href else None

def new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    # light cookie to reduce chance of cookie overlay
    s.cookies.set("CookieConsent", "true", domain="www.medi-learn.de")
    return s

def soup_from(session: requests.Session, url: str) -> BeautifulSoup:
    r = session.get(url, timeout=30)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")

# ---------- Form helpers (based on your DOM notes) ----------

def find_filter_form(soup: BeautifulSoup) -> Optional[BeautifulSoup]:
    """
    Return the <form> that contains the 'jobboerseauswahl' select
    (misleading name), where FppUni / FppFach live.
    """
    # 1) direct: element with id/name 'jobboerseauswahl'
    job_el = soup.find(lambda tag: tag.name in ("select", "input") and (
        (tag.get("id") and "jobboerseauswahl" in tag.get("id", "").lower()) or
        (tag.get("name") and "jobboerseauswahl" in tag.get("name", "").lower())
    ))
    if job_el:
        return job_el.find_parent("form")

    # 2) fallback: any select whose options/text mention Jobbörse
    for frm in soup.find_all("form"):
        if frm.find(string=re.compile(r"jobb[oö]rse", re.I)) or frm.find(
            lambda tag: tag.name == "select" and "jobboerse" in " ".join(
                o.get_text(" ", strip=True).lower() for o in tag.find_all("option")
            )
        ):
            return frm

    # 3) last fallback: first form on the page
    forms = soup.find_all("form")
    return forms[0] if forms else None

def set_text_or_select_value(
    element: BeautifulSoup,
    wanted_text: str
) -> Tuple[str, str]:
    """
    If 'element' is a <select>, pick option by visible/contains.
    If it's an <input>, return (name, wanted_text).
    Returns (field_name, field_value).
    """
    name = element.get("name")
    if not name:
        # try id as name
        name = element.get("id", "")

    tag = element.name.lower()

    if tag == "select":
        opts = element.find_all("option")
        wt_norm = wanted_text.strip().lower()
        # exact (normalized)
        for o in opts:
            vis = (o.get_text(strip=True) or "").strip()
            val = o.get("value", vis) or vis
            if vis.lower() == wt_norm or val.lower() == wt_norm:
                return name, val
        # contains
        for o in opts:
            vis = (o.get_text(strip=True) or "").strip()
            val = o.get("value", vis) or vis
            if wt_norm in vis.lower() or wt_norm in val.lower():
                return name, val
        # fallback: first non-empty
        for o in opts:
            vis = (o.get_text(strip=True) or "").strip()
            val = o.get("value", vis) or vis
            if vis:
                return name, val
        return name, wanted_text

    # input/textarea: send text as-is
    return name, wanted_text

def collect_hidden(form: BeautifulSoup) -> Dict[str, str]:
    data: Dict[str, str] = {}
    for el in form.select("input[type=hidden]"):
        n = el.get("name")
        if n:
            data[n] = el.get("value", "") or ""
    return data

def submit_filters(
    session: requests.Session,
    start_soup: BeautifulSoup,
    uni_text: str,
    fach_text: str,
) -> BeautifulSoup:
    """
    Locate the correct form, fill FppUni and FppFach, POST, return results soup.
    """
    form = find_filter_form(start_soup)
    if not form:
        raise RuntimeError("Filter-Formular nicht gefunden.")

    action = form.get("action") or LIST_URL
    action_url = absolute_url(LIST_URL, action) or LIST_URL

    payload = collect_hidden(form)

    # Your mapping:
    #  - first TR, class="spalte2", id="FppUni" → UNI
    #  - second TR, class="spalte2", id="FppFach" → FACH
    fpp_uni = form.find(id="FppUni")
    fpp_fach = form.find(id="FppFach")

    if not fpp_uni or not fpp_fach:
        # tolerate slight variants (lowercase ids or within class spalte2)
        fpp_uni = form.find(lambda t: t.get("id","").lower() == "fppuni") or \
                  form.select_one('.spalte2 [id="FppUni"], .spalte2 #fppuni')
        fpp_fach = form.find(lambda t: t.get("id","").lower() == "fppfach") or \
                   form.select_one('.spalte2 [id="FppFach"], .spalte2 #fppfach')

    if not fpp_uni or not fpp_fach:
        raise RuntimeError("Felder FppUni/FppFach nicht gefunden.")

    uni_name, uni_val = set_text_or_select_value(fpp_uni, uni_text)
    fach_name, fach_val = set_text_or_select_value(fpp_fach, fach_text)

    if not uni_name or not fach_name:
        raise RuntimeError("Keine Feldnamen für FppUni/FppFach erkannt.")

    payload[uni_name] = uni_val
    payload[fach_name] = fach_val

    # include submit button if present
    submit = form.find("input", {"type": "submit"})
    if submit and submit.get("name"):
        payload[submit["name"]] = submit.get("value", "Suchen")

    resp = session.post(action_url, data=payload, timeout=30)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")

# ---------- Results parsing ----------

def find_results_table(soup: BeautifulSoup) -> Optional[BeautifulSoup]:
    tbl = soup.find("table", id=re.compile(r"^diensttabelle$", re.I))
    if tbl:
        return tbl
    return soup.find("table", class_=re.compile(r"diensttabelle", re.I))

def extract_rows_from_table(table: BeautifulSoup) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    for a in table.find_all("a", href=True):
        m = DETAIL_RE.search(a["href"])
        if not m:
            continue
        ml_id = m.group(1)
        url = absolute_url(LIST_URL, a["href"])
        title = a.get_text(" ", strip=True) or f"Protokoll {ml_id}"
        if url:
            rows.append({"ml_id": ml_id, "title": title, "url": url})
    # de-dup by id
    uniq, seen = [], set()
    for r in rows:
        if r["ml_id"] in seen:
            continue
        seen.add(r["ml_id"])
        uniq.append(r)
    return uniq

def find_next_url(soup: BeautifulSoup) -> Optional[str]:
    # try rel=next first
    a = soup.find("a", attrs={"rel": re.compile(r"\bnext\b", re.I)})
    if a and a.get("href"):
        return absolute_url(LIST_URL, a["href"])
    # then labels
    for cand in soup.find_all("a", href=True):
        txt = cand.get_text(" ", strip=True).lower()
        if any(t in txt for t in ["weiter", "nächste", "next", "»", "›", ">"]):
            full = absolute_url(LIST_URL, cand["href"])
            if full and LIST_PATH in full:
                return full
    return None

def page_contains_filters(session: requests.Session, url: str, uni: str, fach: str) -> bool:
    r = session.get(url, timeout=30)
    if r.status_code != 200:
        return False
    low = BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True).lower()
    return uni.lower() in low and fach.lower() in low

def crawl_count(session: requests.Session, first_results: BeautifulSoup,
                uni: str, fach: str, max_pages: int, pause_pages: float,
                pause_details: float, debug: bool=False) -> pd.DataFrame:
    collected: Dict[str, Dict[str, str]] = {}

    # page 1
    tbl = find_results_table(first_results)
    if debug:
        st.write("Seite 1:", "diensttabelle gefunden" if tbl else "KEINE diensttabelle")
    if tbl:
        for r in extract_rows_from_table(tbl):
            collected[r["ml_id"]] = r

    # pagination
    pages = 1
    next_url = find_next_url(first_results)
    while next_url and pages < max_pages:
        pages += 1
        if pause_pages:
            time.sleep(pause_pages)
        soup = soup_from(session, next_url)
        tbl = find_results_table(soup)
        if debug:
            st.write(f"Seite {pages}:", "diensttabelle gefunden" if tbl else "KEINE diensttabelle", next_url)
        if not tbl:
            break
        for r in extract_rows_from_table(tbl):
            collected[r["ml_id"]] = r
        next_url = find_next_url(soup)

    # open details and filter
    out = []
    for row in collected.values():
        if page_contains_filters(session, row["url"], uni, fach):
            out.append(row)
        if pause_details:
            time.sleep(pause_details)

    if not out:
        return pd.DataFrame(columns=["ml_id", "title", "url"])
    df = pd.DataFrame(out).sort_values("ml_id", key=lambda s: s.astype(int))
    return df.reset_index(drop=True)

# ---------- UI ----------

st.set_page_config(page_title="Medi-Learn Protokolle Counter", page_icon="🩺", layout="wide")
st.title("🩺 Medi-Learn Facharzt-Prüfungsprotokolle — Form POST (FppUni / FppFach)")
st.caption("Befüllt FppUni/FppFach im 'jobboerseauswahl'-Formular, liest 'diensttabelle', paginiert und zählt Treffer.")

with st.sidebar:
    st.header("Filter (sichtbarer Text)")
    uni_text = st.text_input("Uni / Ort (FppUni)", value="Dresden")
    fach_text = st.text_input("Fach (FppFach)", value="Innere Medizin")

    st.header("Crawl")
    max_pages = st.slider("Max. Ergebnisseiten", 1, 60, 20)
    pause_pages = st.slider("Pause zw. Seiten (s)", 0.0, 2.0, 0.2, 0.1)
    pause_details = st.slider("Pause zw. Detail-Seiten (s)", 0.0, 1.0, 0.05, 0.05)
    debug = st.checkbox("Debug-Logs anzeigen", value=False)

    go = st.button("📤 Formular absenden & zählen")

if go:
    try:
        sess = new_session()
        start = soup_from(sess, LIST_URL)

        with st.spinner("Sende Formular mit FppUni / FppFach…"):
            first_results = submit_filters(sess, start, uni_text.strip(), fach_text.strip())

        with st.spinner("Lese 'diensttabelle' + Pagination…"):
            df = crawl_count(
                sess,
                first_results,
                uni=uni_text.strip(),
                fach=fach_text.strip(),
                max_pages=max_pages,
                pause_pages=pause_pages,
                pause_details=pause_details,
                debug=debug,
            )

        st.subheader("Ergebnis")
        st.metric("Anzahl passender Protokolle", len(df))
        if not df.empty:
            st.dataframe(df, use_container_width=True)
            csv = df.to_csv(index=False).encode("utf-8")
            st.download_button(
                "⬇️ CSV herunterladen",
                data=csv,
                file_name=f"medi_learn_{uni_text}_{fach_text}_{len(df)}.csv",
                mime="text/csv",
            )
        else:
            st.info("Keine Treffer – prüfe Schreibweise oder erhöhe Seitenlimit.")

    except Exception as exc:
        st.error(f"Fehler: {exc}")
