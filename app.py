# streamlit_app.py
# Run locally:  streamlit run streamlit_app.py

import io
import time
import csv
import json
import re
from urllib.parse import urljoin, urlencode

import requests
from bs4 import BeautifulSoup
import pandas as pd
import streamlit as st

st.set_page_config(page_title="Medi-Learn Facharzt-Protokolle Scraper", layout="wide")

BASE_URL = "https://www.medi-learn.de/pruefungsprotokolle/facharztpruefung/"
HEADERS = {
    # A realistic browser-like header helps the site return the same HTML you see in Chrome
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
    "Connection": "keep-alive",
}


# -------------------------
# HTTP & discovery helpers
# -------------------------

@st.cache_data(show_spinner=False)
def get_soup(url: str) -> BeautifulSoup:
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


def find_select_value_by_label(soup: BeautifulSoup, select_name: str, text_match: str) -> str | None:
    sel = soup.find("select", attrs={"name": select_name})
    if not sel:
        return None
    wanted = (text_match or "").casefold()
    for opt in sel.find_all("option"):
        visible = (opt.get_text(strip=True) or "").casefold()
        if wanted in visible:
            val = opt.get("value")
            if val:
                return val
    return None


@st.cache_data(show_spinner=False)
def discover_filter_values() -> dict:
    """Fetch the search page and collect visible labels & internal values for Uni/Fach."""
    soup = get_soup(BASE_URL)
    uni_opts, fach_opts = [], []

    sel_uni = soup.find("select", attrs={"name": "FppUni"})
    if sel_uni:
        for o in sel_uni.find_all("option"):
            t, v = o.get_text(strip=True), o.get("value")
            if v:
                uni_opts.append((t, v))

    sel_fach = soup.find("select", attrs={"name": "FppFach"})
    if sel_fach:
        for o in sel_fach.find_all("option"):
            t, v = o.get_text(strip=True), o.get("value")
            if v:
                fach_opts.append((t, v))

    return {"uni": uni_opts, "fach": fach_opts}


def build_results_url(uni_val: str, fach_val: str, page_nr: int = 1, page_len: int = 40) -> str:
    """
    Listing accepts GET params; the visible table may be embedded.
    """
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
    Fetch the visible list page. If the results live in an embedded frame (e.g. FacharztProtokolle.php),
    follow that frame and return the inner HTML (the one that actually contains the links).
    """
    # Top-level list page
    r = requests.get(list_url, headers={**HEADERS, "Referer": BASE_URL}, timeout=30)
    r.raise_for_status()
    html = r.text
    soup = BeautifulSoup(html, "html.parser")

    # Try to find an iframe or embedded results container
    # If there is an <iframe src="...FacharztProtokolle.php?...">, fetch it:
    iframe = soup.find("iframe")
    if iframe and iframe.get("src"):
        inner_url = urljoin(BASE_URL, iframe["src"])
        r2 = requests.get(inner_url, headers={**HEADERS, "Referer": list_url}, timeout=30)
        if r2.ok and r2.text:
            return r2.text

    # If not an iframe, the table might be directly in the returned HTML
    return html


def extract_detail_links_from_listing_html(html: str) -> list[str]:
    """
    Be robust: capture normal anchors and also any plain-text/onclick patterns for detailed.php?ID=...
    """
    links: list[str] = []

    # 1) Normal <a href="detailed.php?ID=...">
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "detailed.php?ID=" in href:
            links.append(href if href.startswith("http") else urljoin(BASE_URL, href))

    # 2) Onclick/plain text fallback with regex
    rx = re.compile(r"detailed\.php\?ID=\d+")
    for m in rx.findall(html):
        links.append(urljoin(BASE_URL, m))

    # De-duplicate while keeping order
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


# -------------------------
# Detail page extraction
# -------------------------

def parse_detail_page(soup: BeautifulSoup, url: str) -> dict:
    """
    Extract common label/value fields and free-text sections like Fragen/Themen and Tipps.
    Targets the structure used by Medi-Learn detail pages such as:
    - .../detailed.php?ID=250  (Dresden, Innere Medizin)
    - .../detailed.php?ID=4308 (Dresden, Innere Medizin)
    - .../detailed.php?ID=4709 (Dresden, Innere Medizin)
    - .../detailed.php?ID=5133 (Dresden, Innere Medizin)
    - .../detailed.php?ID=5744 (Dresden, Innere Medizin)
    - .../detailed.php?ID=3899 (Dresden, Innere/Allgemeinmedizin)
    """
    data: dict[str, str] = {"detail_url": url}

    label_candidates = [
        "Ort/Uni", "Fach", "Prüfer", "Atmosphäre", "Dauer", "Note",
        "Datum", "Erstellt", "Prüfungsjahr", "Prüfungsort", "Prüfungsdatum",
        "Vorgespräch"
    ]

    # Scan simple "Label: Value" patterns in common containers
    text_blocks = []
    for el in soup.find_all(["p", "li", "div", "td"]):
        t = el.get_text(" ", strip=True)
        if ":" in t:
            text_blocks.append(t)

    for tb in text_blocks:
        # Allow multiple pairs separated by extra spaces
        segments = [x.strip() for x in re.split(r"\s{2,}", tb)]
        for seg in segments:
            if ":" in seg:
                lab, val = seg.split(":", 1)
                lab, val = lab.strip(), val.strip()
                if any(lab.startswith(L) for L in label_candidates):
                    data[lab] = val

    # Try to collect sections under headings like "Fragen", "Themen/Fragen", "Tipps"
    def collect_section(headings: list[str]) -> str | None:
        candidates = soup.find_all(["h1", "h2", "h3", "strong", "b"])
        for h in candidates:
            ht = h.get_text(" ", strip=True)
            for target in headings:
                if target.casefold() in (ht or "").casefold():
                    # Gather subsequent siblings in the same parent until another heading-like appears
                    parent = h.parent if h.parent else soup
                    texts = []
                    for sib in parent.find_all(recursive=False):
                        if sib is h:
                            continue
                        # stop at next bold/heading-like chunk
                        if sib.find(["strong", "b"]) or sib.name in ["h1", "h2", "h3"]:
                            break
                        texts.append(sib.get_text(" ", strip=True))
                    out = "\n".join([t for t in texts if t])
                    if out:
                        return out
        return None

    fragen = collect_section(["Fragen", "Themen", "Themen/Fragen"])
    tipps = collect_section(["Tipps", "Hinweise", "Empfehlungen"])

    if fragen:
        data["Fragen_Themen"] = fragen
    if tipps:
        data["Tipps"] = tipps

    # Page title as a fallback field
    if (title := soup.find("title")):
        data["title"] = title.get_text(strip=True)

    return data


# -------------------------
# Scrape driver
# -------------------------

def scrape(uni_label: str, fach_label: str, max_pages: int, delay_sec: float) -> list[dict]:
    # Resolve visible labels -> internal option values
    search_soup = get_soup(BASE_URL)
    time.sleep(delay_sec)

    uni_val = find_select_value_by_label(search_soup, "FppUni", uni_label) or ""
    fach_val = find_select_value_by_label(search_soup, "FppFach", fach_label) or ""
    if not uni_val:
        raise RuntimeError(f"Could not resolve Uni option value for '{uni_label}'.")
    if not fach_val:
        raise RuntimeError(f"Could not resolve Fach option value for '{fach_label}'.")

    all_records: list[dict] = []
    page = 1
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


# -------------------------
# UI
# -------------------------

st.title("Medi-Learn Facharzt-Prüfungsprotokolle Scraper")
st.caption(
    "Filter → Liste → jede **Details**-Seite öffnen → Felder extrahieren → Vorschau → Download CSV/JSON.\n"
    "Voreinstellung: **Dresden** (Uni) und **Innere Medizin** (Fach)."
)

with st.sidebar:
    st.header("Filter")
    opts = discover_filter_values()

    uni_labels = [t for t, _ in opts.get("uni", [])]
    fach_labels = [t for t, _ in opts.get("fach", [])]

    default_uni = next((i for i, t in enumerate(uni_labels) if "dresden" in t.lower()), 0) if uni_labels else 0
    default_fach = next((i for i, t in enumerate(fach_labels) if "innere" in t.lower()), 0) if fach_labels else 0

    uni_choice = st.selectbox("Universität (Anzeige-Text)", uni_labels or ["—"], index=default_uni)
    fach_choice = st.selectbox("Fach (Anzeige-Text)", fach_labels or ["—"], index=default_fach)

    max_pages = st.number_input("Max. Seiten durchsuchen", min_value=1, max_value=50, value=10, step=1)
    delay_sec = st.slider("Pausenzeit pro Anfrage (Sekunden)", min_value=0.0, max_value=3.0, value=1.0, step=0.1)

    run = st.button("Scrape starten", type="primary")

if run:
    try:
        records = scrape(uni_choice, fach_choice, max_pages=int(max_pages), delay_sec=float(delay_sec))
        st.success(f"Fertig. {len(records)} Protokolle extrahiert.")

        if records:
            df = pd.DataFrame(records)
            st.dataframe(df, use_container_width=True)

            # JSON download
            json_bytes = json.dumps(records, ensure_ascii=False, indent=2).encode("utf-8")
            st.download_button("Download JSON", data=json_bytes, file_name="medi_learn_protokolle.json", mime="application/json")

            # CSV download (union of keys)
            keys = sorted({k for r in records for k in r.keys()})
            sio = io.StringIO()
            writer = csv.DictWriter(sio, fieldnames=keys)
            writer.writeheader()
            for r in records:
                writer.writerow(r)
            st.download_button("Download CSV", data=sio.getvalue().encode("utf-8"),
                               file_name="medi_learn_protokolle.csv", mime="text/csv")
        else:
            st.info("Keine Datensätze gefunden. Bitte Filter prüfen oder die Seitenanzahl erhöhen.")

    except Exception as e:
        st.error(f"Fehler: {e}")

with st.expander("Hinweise & Ethik"):
    st.markdown(
        "- Die App **löst die Auswahlwerte dynamisch auf** (FppUni/FppFach) von der öffentlichen Suchseite.\n"
        "- Ergebnisse können in einer **eingebetteten Seite** gerendert werden; deshalb wird die **innere HTML-Quelle** geladen.\n"
        "- Die Extraktion zielt auf Felder wie *Ort/Uni*, *Fach*, *Prüfer*, *Atmosphäre*, *Dauer*, *Note* sowie Abschnitte *Fragen/Themen* und *Tipps* – siehe Beispiel-Detailseiten.\n"
        "- Bitte beachte die Nutzungsbedingungen der Zielseite und setze eine **angemessene Pause** zwischen Anfragen."
    )
