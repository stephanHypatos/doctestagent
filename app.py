import re
import time
import html
import urllib.parse
import requests
import pandas as pd
from bs4 import BeautifulSoup
import streamlit as st

# ======================================
# Settings
# ======================================
BASE_URL = "https://www.medi-learn.de"
START_PATH = "/pruefungsprotokolle/facharztpruefung/"
START_URL = urllib.parse.urljoin(BASE_URL, START_PATH)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0.0.0 Safari/537.36")

HEADERS = {"User-Agent": UA}
DETAIL_RE = re.compile(r"detailed\.php\?ID=(\d+)", re.I)

# ======================================
# Utilities
# ======================================
def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()

def absolute_url(base: str, href: str | None) -> str | None:
    if not href:
        return None
    return urllib.parse.urljoin(base, href)

def get_soup(session: requests.Session, url: str) -> BeautifulSoup:
    r = session.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return BeautifulSoup(r.text, "lxml")

def get_forms(soup: BeautifulSoup):
    return soup.find_all("form")

def form_has_filters(form: BeautifulSoup) -> bool:
    """Heuristic: does this form look like the one with Uni/Fach filters?"""
    txt = form.get_text(" ", strip=True).lower()
    # Look for typical German labels/keywords around those filters
    needles = ["uni", "universität", "standort", "ort", "fach", "fachgebiet", "innere", "medizin"]
    score = sum(1 for n in needles if n in txt)
    # Also check selects count
    has_selects = bool(form.find_all("select"))
    return has_selects and score >= 2

def collect_hidden_inputs(form: BeautifulSoup) -> dict:
    data = {}
    for el in form.select("input[type=hidden]"):
        name = el.get("name")
        if not name:
            continue
        data[name] = el.get("value", "")
    return data

def option_map_by_text(select: BeautifulSoup) -> dict:
    """
    Return {normalized_visible_text: (value, visible_text)} for all <option>.
    """
    out = {}
    for opt in select.find_all("option"):
        vis = opt.get_text(strip=True)
        val = opt.get("value", vis)
        if not vis:
            continue
        out[norm(vis)] = (val, vis)
    return out

def find_selects(form: BeautifulSoup):
    selects = form.find_all("select")
    return selects

def guess_select_role(select: BeautifulSoup) -> str | None:
    """
    Try to guess if a select is for 'uni' or 'fach' based on label, name, id, or option texts.
    """
    name = (select.get("name") or "").lower()
    sid = (select.get("id") or "").lower()
    around = select.find_parent().get_text(" ", strip=True).lower()

    uni_tokens = ["uni", "universität", "standort", "ort", "tu dresden", "dresden"]
    fach_tokens = ["fach", "fachgebiet", "innere", "medizin", "chirurgie", "neurologie"]

    # name/id heuristic
    if any(t in name or t in sid for t in ["uni", "standort", "ort"]):
        return "uni"
    if any(t in name or t in sid for t in ["fach", "fachgebiet", "gebiet"]):
        return "fach"

    # nearby text heuristic
    if any(t in around for t in uni_tokens):
        return "uni"
    if any(t in around for t in fach_tokens):
        return "fach"

    # option-content heuristic: if many options look like cities/unis vs. specialties
    opts = [norm(o.get_text(strip=True)) for o in select.find_all("option")]
    city_hits = sum(1 for o in opts if any(c in o for c in ["berlin", "münchen", "hamburg", "dresden", "köln", "hannover", "frankfurt"]))
    fach_hits = sum(1 for o in opts if any(f in o for f in ["innere", "chirurgie", "neurologie", "anästhesie", "derma", "gyn"]))
    if city_hits > fach_hits:
        return "uni"
    if fach_hits > city_hits:
        return "fach"
    return None

def pick_option_value(select: BeautifulSoup, wanted_text: str) -> tuple[str | None, str | None]:
    """
    Match user 'wanted_text' against visible option text (case/space-insensitive).
    Returns (value, visible_text) or (None, None).
    """
    omap = option_map_by_text(select)
    key = norm(wanted_text)
    # direct
    if key in omap:
        return omap[key]
    # fuzzy: startswith or contains
    for k, (v, vis) in omap.items():
        if k.startswith(key) or key in k or k in key:
            return v, vis
    return None, None

def extract_result_rows(soup: BeautifulSoup) -> list[dict]:
    """
    Extract links to detailed.php?ID=… and some small info from the result page.
    """
    rows = []
    # Strategy: any anchor that contains detailed.php?ID=…
    for a in soup.find_all("a", href=True):
        m = DETAIL_RE.search(a["href"])
        if not m:
            continue
        ml_id = m.group(1)
        url = absolute_url(START_URL, a["href"])
        title = a.get_text(" ", strip=True) or f"Protokoll {ml_id}"
        rows.append({"ml_id": ml_id, "title": title, "url": url})
    # De-dup by ml_id
    seen = set()
    uniq = []
    for r in rows:
        if r["ml_id"] in seen:
            continue
        seen.add(r["ml_id"])
        uniq.append(r)
    return uniq

def find_next_page_url(soup: BeautifulSoup) -> str | None:
    """
    Try to find 'next' pagination link (weiter, nächste, >, etc.).
    """
    # Look for anchors with typical next labels
    candidates = []
    for a in soup.find_all("a", href=True):
        txt = a.get_text(" ", strip=True).lower()
        if any(t in txt for t in ["weiter", "nächste", "next", "»", "›", ">"]):
            candidates.append(a["href"])
    if not candidates:
        return None
    # Prefer the first that stays within the same tool path
    for href in candidates:
        full = absolute_url(START_URL, href)
        if "/pruefungsprotokolle/facharztpruefung" in full:
            return full
    return absolute_url(START_URL, candidates[0])

def submit_filter(session: requests.Session, start_soup: BeautifulSoup, uni_text: str, fach_text: str) -> tuple[list[dict], BeautifulSoup]:
    """
    Locate the filter form, select the desired Uni & Fach by visible text,
    POST it, parse the first result page, and return (rows, soup).
    """
    forms = get_forms(start_soup)
    target_form = None
    for f in forms:
        if form_has_filters(f):
            target_form = f
            break
    if not target_form:
        # fallback: first form
        if forms:
            target_form = forms[0]
        else:
            raise RuntimeError("No <form> found on start page.")

    action = target_form.get("action") or START_URL
    action_url = absolute_url(START_URL, action)

    payload = collect_hidden_inputs(target_form)

    # Identify selects and pick values
    selects = find_selects(target_form)
    uni_name = fach_name = None
    uni_val = fach_val = None
    for sel in selects:
        role = guess_select_role(sel)
        if role == "uni":
            v, vis = pick_option_value(sel, uni_text)
            if v:
                uni_val = v
                uni_name = sel.get("name")
        elif role == "fach":
            v, vis = pick_option_value(sel, fach_text)
            if v:
                fach_val = v
                fach_name = sel.get("name")

    # If roles weren’t guessed, try best-effort by presence of the desired text in options
    if not uni_val:
        for sel in selects:
            v, vis = pick_option_value(sel, uni_text)
            if v:
                uni_val = v
                uni_name = sel.get("name")
                break

    if not fach_val:
        for sel in selects:
            v, vis = pick_option_value(sel, fach_text)
            if v:
                fach_val = v
                fach_name = sel.get("name")
                break

    if not uni_name or not fach_name or not uni_val or not fach_val:
        raise RuntimeError(
            f"Could not map both filters. "
            f"Resolved -> uni: name={uni_name} val={uni_val}, fach: name={fach_name} val={fach_val}"
        )

    payload[uni_name] = uni_val
    payload[fach_name] = fach_val

    # Try to detect submit button name/value (some forms require it)
    submit = target_form.find("input", {"type": "submit"})
    if submit and submit.get("name"):
        payload[submit["name"]] = submit.get("value", "Suchen")

    # Send POST
    r = session.post(action_url, data=payload, headers=HEADERS, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    rows = extract_result_rows(soup)
    return rows, soup

def crawl_all_pages(session: requests.Session, first_soup: BeautifulSoup, pause_s: float = 0.5) -> list[dict]:
    """
    Starting from the first results page soup, follow pagination and aggregate rows.
    """
    all_rows = extract_result_rows(first_soup)
    seen_ids = {r["ml_id"] for r in all_rows}

    next_url = find_next_page_url(first_soup)
    safety = 0
    while next_url and safety < 30:
        safety += 1
        time.sleep(pause_s)
        r = session.get(next_url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        rows = extract_result_rows(soup)
        for r_ in rows:
            if r_["ml_id"] not in seen_ids:
                seen_ids.add(r_["ml_id"])
                all_rows.append(r_)
        next_url = find_next_page_url(soup)
    # sort by id numeric
    all_rows.sort(key=lambda x: int(x["ml_id"]))
    return all_rows

def fetch_detail_fields(session: requests.Session, url: str) -> dict:
    """
    Optional enrichment: fetch each detail page and try to extract 'Uni' and 'Fach'
    and maybe Atmosphere/Prüfer fields for quick display.
    """
    try:
        r = session.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        text = soup.get_text(" ", strip=True)
        low = text.lower()

        def grab(label_variants):
            for lab in label_variants:
                m = re.search(lab, low)
                if m:
                    # take the next ~150 chars as snippet
                    start = m.end()
                    snippet = text[start:start+150]
                    snippet = re.split(r"[\n\r|•\-]{2,}|  ", snippet)[0]
                    return snippet.strip(":; \n\r\t")
            return ""

        uni_guess = grab([r"\buni\b", r"universit[aä]t", r"dresden"])
        fach_guess = grab([r"\bfach\b", r"fachgebiet", r"innere", r"medizin"])
        atm = grab([r"atmosph[aä]re", r"stimmung"])
        pruefer = grab([r"pr[üu]fer", r"vorsitz", r"kommission"])

        title = soup.title.get_text(strip=True) if soup.title else url
        return {
            "title_detail": title,
            "uni_guess": uni_guess,
            "fach_guess": fach_guess,
            "atmosphaere": atm,
            "pruefer": pruefer
        }
    except Exception:
        return {
            "title_detail": "",
            "uni_guess": "",
            "fach_guess": "",
            "atmosphaere": "",
            "pruefer": ""
        }

# ======================================
# Streamlit UI
# ======================================
st.set_page_config(page_title="Medi-Learn Protokolle – Form Scraper", page_icon="🩺", layout="wide")
st.title("🩺 Medi-Learn Facharzt-Prüfungsprotokolle — Form-Submission Scraper")
st.caption("Posts the real Medi-Learn filter form (no search engines). Handles pagination and counts matching Protokolle.")

with st.sidebar:
    st.header("Filter")
    uni = st.text_input("Universität (sichtbarer Text)", value="Dresden", help="Geben Sie den sichtbaren Uni-Text ein, z. B. „Dresden“, „TU Dresden“, „Uniklinikum Dresden“.")
    fach = st.text_input("Fach (sichtbarer Text)", value="Innere Medizin", help="Z. B. „Innere Medizin“.")
    enrich = st.checkbox("Optional: Details aus jeder Protokoll-Seite lesen (langsamer)", value=False)
    pause = st.slider("Pausenzeit bei Pagination (Sek.)", 0.0, 2.0, 0.5, 0.1)
    go = st.button("📤 Formular absenden & Zählen")

st.markdown(
    """
**Hinweis:** Dieses Tool versucht automatisch die richtigen Formularfelder (Uni/Fach) zu erkennen, wählt die Option \
entsprechend dem **sichtbaren Text** und sendet dann die Anfrage ab.
"""
)

if go:
    try:
        with st.spinner("Lade Startseite & analysiere Formular…"):
            sess = requests.Session()
            sess.headers.update(HEADERS)
            start_soup = get_soup(sess, START_URL)

        with st.spinner("Sende Formular & lese erste Ergebnisse…"):
            rows, first_soup = submit_filter(sess, start_soup, uni_text=uni.strip(), fach_text=fach.strip())

        with st.spinner("Folge Pagination (falls vorhanden)…"):
            all_rows = crawl_all_pages(sess, first_soup, pause_s=pause)

        # If the first submit already returned results that might not be included in pagination-driven crawl,
        # merge them as well:
        ids_seen = {r["ml_id"] for r in all_rows}
        for r0 in rows:
            if r0["ml_id"] not in ids_seen:
                all_rows.append(r0)
                ids_seen.add(r0["ml_id"])
        all_rows.sort(key=lambda x: int(x["ml_id"]))

        st.subheader("Ergebnis")
        st.metric("Anzahl Protokolle", len(all_rows))

        if not all_rows:
            st.info("Keine Treffer. Tipp: Passen Sie den sichtbaren Text der Filter an (z. B. „TU Dresden“ oder „Innere“).")
        else:
            df = pd.DataFrame(all_rows)

            if enrich:
                with st.spinner("Lese Details aus jeder Protokoll-Seite…"):
                    extra = []
                    for i, row in enumerate(all_rows, start=1):
                        st.write(f"Detail {i}/{len(all_rows)} – {row['url']}")
                        extra.append(fetch_detail_fields(sess, row["url"]))
                        time.sleep(0.15)
                    extra_df = pd.DataFrame(extra)
                    df = pd.concat([df.reset_index(drop=True), extra_df.reset_index(drop=True)], axis=1)

            # order columns nicely
            base_cols = ["ml_id", "title", "url"]
            extra_cols = [c for c in df.columns if c not in base_cols]
            df = df[base_cols + extra_cols]

            st.dataframe(df, use_container_width=True)

            csv = df.to_csv(index=False).encode("utf-8")
            st.download_button(
                "⬇️ CSV herunterladen",
                data=csv,
                file_name=f"medi_learn_protokolle_form_{norm(uni).replace(' ','_')}_{norm(fach).replace(' ','_')}.csv",
                mime="text/csv"
            )

        st.divider()
        st.markdown(
            """
            **Tipps:**
            - Nutzen Sie exakt den **sichtbaren Text** der Optionen (z. B. „Innere Medizin“, nicht „Innere“ – je nach Seite).
            - Probieren Sie Varianten wie „TU Dresden“ vs. „Dresden“.
            - Wenn die Seite einen CSRF-Token oder zwingende „submit“-Feldnamen verlangt, werden diese automatisch aus dem Formular übernommen.
            """
        )

    except Exception as e:
        st.error(f"Fehler: {e}")
