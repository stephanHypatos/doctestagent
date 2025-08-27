from __future__ import annotations

import re
import time
import urllib.parse
from typing import Dict, List, Optional, Tuple

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup

# ======================================
# Settings
# ======================================

BASE_URL = "https://www.medi-learn.de"
START_PATH = "/pruefungsprotokolle/facharztpruefung/"
START_URL = urllib.parse.urljoin(BASE_URL, START_PATH)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

HEADERS = {"User-Agent": UA}
DETAIL_RE = re.compile(r"detailed\.php\?ID=(\d+)", re.I)


# ======================================
# Utilities
# ======================================

def norm(s: str) -> str:
    """Normalize whitespace and lowercase."""
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


def absolute_url(base: str, href: Optional[str]) -> Optional[str]:
    """Build absolute URL from base + href."""
    if not href:
        return None
    return urllib.parse.urljoin(base, href)


def get_soup(session: requests.Session, url: str) -> BeautifulSoup:
    """GET a URL and parse with the built-in HTML parser (no lxml dependency)."""
    resp = session.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


def get_forms(soup: BeautifulSoup) -> List[BeautifulSoup]:
    """Return all <form> elements."""
    return list(soup.find_all("form"))


def form_has_filters(form: BeautifulSoup) -> bool:
    """Heuristic: does a form look like the Uni/Fach filter form?"""
    txt = form.get_text(" ", strip=True).lower()
    needles = [
        "uni",
        "universität",
        "standort",
        "ort",
        "fach",
        "fachgebiet",
        "innere",
        "medizin",
    ]
    score = sum(1 for n in needles if n in txt)
    has_selects = bool(form.find_all("select"))
    return has_selects and score >= 2


def collect_hidden_inputs(form: BeautifulSoup) -> Dict[str, str]:
    """Collect hidden input fields as a payload base (incl. CSRF tokens)."""
    data: Dict[str, str] = {}
    for el in form.select("input[type=hidden]"):
        name = el.get("name")
        if not name:
            continue
        data[name] = el.get("value", "") or ""
    return data


def option_map_by_text(select: BeautifulSoup) -> Dict[str, Tuple[str, str]]:
    """
    Map normalized visible text -> (value, visible_text) for all <option>.
    """
    out: Dict[str, Tuple[str, str]] = {}
    for opt in select.find_all("option"):
        vis = (opt.get_text(strip=True) or "").strip()
        if not vis:
            continue
        val = opt.get("value", vis) or vis
        out[norm(vis)] = (val, vis)
    return out


def find_selects(form: BeautifulSoup) -> List[BeautifulSoup]:
    """Return all <select> elements in a form."""
    return list(form.find_all("select"))


def guess_select_role(select: BeautifulSoup) -> Optional[str]:
    """
    Guess if a <select> is for 'uni' or 'fach' based on name/id/nearby text/options.
    Returns 'uni', 'fach', or None if unsure.
    """
    name = (select.get("name") or "").lower()
    sel_id = (select.get("id") or "").lower()
    around = select.find_parent().get_text(" ", strip=True).lower()

    uni_tokens = ["uni", "universität", "standort", "ort", "tu dresden", "dresden"]
    fach_tokens = ["fach", "fachgebiet", "innere", "medizin", "chirurgie", "neurologie"]

    if any(t in name or t in sel_id for t in ["uni", "standort", "ort"]):
        return "uni"
    if any(t in name or t in sel_id for t in ["fach", "fachgebiet", "gebiet"]):
        return "fach"

    if any(t in around for t in uni_tokens):
        return "uni"
    if any(t in around for t in fach_tokens):
        return "fach"

    # Option-content heuristic
    opts = [norm(o.get_text(strip=True)) for o in select.find_all("option")]
    city_hits = sum(
        1
        for o in opts
        if any(c in o for c in ["berlin", "münchen", "hamburg", "dresden", "köln", "hannover", "frankfurt"])
    )
    fach_hits = sum(
        1
        for o in opts
        if any(f in o for f in ["innere", "chirurgie", "neurologie", "anästhesie", "derma", "gyn"])
    )
    if city_hits > fach_hits:
        return "uni"
    if fach_hits > city_hits:
        return "fach"
    return None


def pick_option_value(
    select: BeautifulSoup, wanted_text: str
) -> Tuple[Optional[str], Optional[str]]:
    """
    Pick an option by visible text (case/space-insensitive).
    Returns (value, visible_text) or (None, None).
    """
    omap = option_map_by_text(select)
    key = norm(wanted_text)
    if key in omap:
        return omap[key]
    for k, (v, vis) in omap.items():
        if k.startswith(key) or key in k or k in key:
            return v, vis
    return None, None


def extract_result_rows(soup: BeautifulSoup) -> List[Dict[str, str]]:
    """Extract unique detailed.php?ID=… links as rows."""
    rows: List[Dict[str, str]] = []
    for a in soup.find_all("a", href=True):
        match = DETAIL_RE.search(a["href"])
        if not match:
            continue
        ml_id = match.group(1)
        url = absolute_url(START_URL, a["href"])
        title = a.get_text(" ", strip=True) or f"Protokoll {ml_id}"
        if not url:
            continue
        rows.append({"ml_id": ml_id, "title": title, "url": url})

    seen: set[str] = set()
    uniq: List[Dict[str, str]] = []
    for r in rows:
        if r["ml_id"] in seen:
            continue
        seen.add(r["ml_id"])
        uniq.append(r)
    return uniq


def find_next_page_url(soup: BeautifulSoup) -> Optional[str]:
    """Try to find a 'next' pagination link."""
    candidates: List[str] = []
    for a in soup.find_all("a", href=True):
        txt = a.get_text(" ", strip=True).lower()
        if any(t in txt for t in ["weiter", "nächste", "next", "»", "›", ">"]):
            candidates.append(a["href"])

    if not candidates:
        return None

    for href in candidates:
        full = absolute_url(START_URL, href)
        if full and "/pruefungsprotokolle/facharztpruefung" in full:
            return full
    return absolute_url(START_URL, candidates[0])


def submit_filter(
    session: requests.Session,
    start_soup: BeautifulSoup,
    uni_text: str,
    fach_text: str,
) -> Tuple[List[Dict[str, str]], BeautifulSoup]:
    """
    Locate the filter form, choose Uni/Fach by visible text, POST it, and parse results.
    Returns (first_rows, first_results_soup).
    """
    forms = get_forms(start_soup)
    target_form: Optional[BeautifulSoup] = None
    for frm in forms:
        if form_has_filters(frm):
            target_form = frm
            break
    if not target_form:
        target_form = forms[0] if forms else None
    if not target_form:
        raise RuntimeError("No <form> found on start page.")

    action = target_form.get("action") or START_URL
    action_url = absolute_url(START_URL, action)
    if not action_url:
        raise RuntimeError("Form action URL could not be resolved.")

    payload = collect_hidden_inputs(target_form)

    selects = find_selects(target_form)
    uni_name: Optional[str] = None
    fach_name: Optional[str] = None
    uni_val: Optional[str] = None
    fach_val: Optional[str] = None

    for sel in selects:
        role = guess_select_role(sel)
        if role == "uni":
            v, _ = pick_option_value(sel, uni_text)
            if v:
                uni_val = v
                uni_name = sel.get("name")
        elif role == "fach":
            v, _ = pick_option_value(sel, fach_text)
            if v:
                fach_val = v
                fach_name = sel.get("name")

    # Fallback: search any select for the desired texts
    if not uni_val:
        for sel in selects:
            v, _ = pick_option_value(sel, uni_text)
            if v:
                uni_val = v
                uni_name = sel.get("name")
                break

    if not fach_val:
        for sel in selects:
            v, _ = pick_option_value(sel, fach_text)
            if v:
                fach_val = v
                fach_name = sel.get("name")
                break

    if not uni_name or not fach_name or not uni_val or not fach_val:
        raise RuntimeError(
            "Could not map both filters. "
            f"Resolved -> uni: name={uni_name} val={uni_val}, "
            f"fach: name={fach_name} val={fach_val}"
        )

    payload[uni_name] = uni_val
    payload[fach_name] = fach_val

    # Some forms require submit button name/value
    submit = target_form.find("input", {"type": "submit"})
    if submit and submit.get("name"):
        payload[submit["name"]] = submit.get("value", "Suchen")

    resp = session.post(action_url, data=payload, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    rows = extract_result_rows(soup)
    return rows, soup


def crawl_all_pages(
    session: requests.Session,
    first_soup: BeautifulSoup,
    pause_s: float = 0.5,
) -> List[Dict[str, str]]:
    """Follow pagination from the first result page and aggregate all rows."""
    all_rows = extract_result_rows(first_soup)
    seen_ids = {r["ml_id"] for r in all_rows}

    next_url = find_next_page_url(first_soup)
    safety = 0
    while next_url and safety < 30:
        safety += 1
        time.sleep(pause_s)
        resp = session.get(next_url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        rows = extract_result_rows(soup)
        for row in rows:
            if row["ml_id"] not in seen_ids:
                seen_ids.add(row["ml_id"])
                all_rows.append(row)
        next_url = find_next_page_url(soup)

    all_rows.sort(key=lambda x: int(x["ml_id"]))
    return all_rows


def fetch_detail_fields(session: requests.Session, url: str) -> Dict[str, str]:
    """
    Optional enrichment: fetch each detail page and try to extract Uni/Fach/Atmosphäre/Prüfer.
    """
    try:
        resp = session.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        text = soup.get_text(" ", strip=True)
        low = text.lower()

        def grab(label_variants: List[str]) -> str:
            for lab in label_variants:
                match = re.search(lab, low)
                if match:
                    start = match.end()
                    snippet = text[start : start + 150]
                    snippet = re.split(r"[\n\r|•\-]{2,}|  ", snippet)[0]
                    return snippet.strip(":; \n\r\t")
            return ""

        uni_guess = grab([r"\buni\b", r"universit[aä]t", r"dresden"])
        fach_guess = grab([r"\bfach\b", r"fachgebiet", r"innere", r"medizin"])
        atmos = grab([r"atmosph[aä]re", r"stimmung"])
        pruefer = grab([r"pr[üu]fer", r"vorsitz", r"kommission"])

        title = soup.title.get_text(strip=True) if soup.title else url
        return {
            "title_detail": title,
            "uni_guess": uni_guess,
            "fach_guess": fach_guess,
            "atmosphaere": atmos,
            "pruefer": pruefer,
        }
    except Exception:
        return {
            "title_detail": "",
            "uni_guess": "",
            "fach_guess": "",
            "atmosphaere": "",
            "pruefer": "",
        }


# ======================================
# Streamlit UI
# ======================================

st.set_page_config(
    page_title="Medi-Learn Protokolle – Form Scraper",
    page_icon="🩺",
    layout="wide",
)
st.title("🩺 Medi-Learn Facharzt-Prüfungsprotokolle — Form-Submission Scraper")
st.caption(
    "Posts the real Medi-Learn filter form (no search engines). "
    "Handles pagination and counts matching Protokolle."
)

with st.sidebar:
    st.header("Filter")
    uni = st.text_input(
        "Universität (sichtbarer Text)",
        value="Dresden",
        help="Z. B. „Dresden“, „TU Dresden“, „Uniklinikum Dresden“.",
    )
    fach = st.text_input(
        "Fach (sichtbarer Text)",
        value="Innere Medizin",
        help="Z. B. „Innere Medizin“.",
    )
    enrich = st.checkbox(
        "Optional: Details aus jeder Protokoll-Seite lesen (langsamer)", value=False
    )
    pause = st.slider("Pausenzeit bei Pagination (Sek.)", 0.0, 2.0, 0.5, 0.1)
    go = st.button("📤 Formular absenden & Zählen")

st.markdown(
    "**Hinweis:** Das Tool erkennt die richtigen Formularfelder (Uni/Fach) automatisch, "
    "wählt anhand des sichtbaren Textes und sendet die Anfrage ab."
)

if go:
    try:
        with st.spinner("Lade Startseite & analysiere Formular…"):
            sess = requests.Session()
            sess.headers.update(HEADERS)
            start_soup = get_soup(sess, START_URL)

        with st.spinner("Sende Formular & lese erste Ergebnisse…"):
            first_rows, first_soup = submit_filter(
                sess, start_soup, uni_text=uni.strip(), fach_text=fach.strip()
            )

        with st.spinner("Folge Pagination (falls vorhanden)…"):
            all_rows = crawl_all_pages(sess, first_soup, pause_s=pause)

        # Merge possibly missing first_rows
        ids_seen = {r["ml_id"] for r in all_rows}
        for r0 in first_rows:
            if r0["ml_id"] not in ids_seen:
                all_rows.append(r0)
                ids_seen.add(r0["ml_id"])
        all_rows.sort(key=lambda x: int(x["ml_id"]))

        st.subheader("Ergebnis")
        st.metric("Anzahl Protokolle", len(all_rows))

        if not all_rows:
            st.info(
                "Keine Treffer. Tipp: Passen Sie den sichtbaren Text der Filter an "
                "(z. B. „TU Dresden“ oder „Innere“)."
            )
        else:
            df = pd.DataFrame(all_rows)

            if enrich:
                with st.spinner("Lese Details aus jeder Protokoll-Seite…"):
                    extra: List[Dict[str, str]] = []
                    for i, row in enumerate(all_rows, start=1):
                        st.write(f"Detail {i}/{len(all_rows)} – {row['url']}")
                        extra.append(fetch_detail_fields(sess, row["url"]))
                        time.sleep(0.15)
                    extra_df = pd.DataFrame(extra)
                    df = pd.concat(
                        [df.reset_index(drop=True), extra_df.reset_index(drop=True)], axis=1
                    )

            base_cols = ["ml_id", "title", "url"]
            extra_cols = [c for c in df.columns if c not in base_cols]
            df = df[base_cols + extra_cols]

            st.dataframe(df, use_container_width=True)

            csv = df.to_csv(index=False).encode("utf-8")
            st.download_button(
                "⬇️ CSV herunterladen",
                data=csv,
                file_name=(
                    f"medi_learn_protokolle_form_"
                    f"{norm(uni).replace(' ', '_')}_"
                    f"{norm(fach).replace(' ', '_')}.csv"
                ),
                mime="text/csv",
            )

        st.divider()
        st.markdown(
            "**Tipps:** Versuchen Sie Varianten des sichtbaren Texts "
            "(z. B. „TU Dresden“), falls die Seite andere Bezeichnungen nutzt."
        )
    except Exception as exc:  # noqa: BLE001
        st.error(f"Fehler: {exc}")
