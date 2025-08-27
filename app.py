import re
import time
import html
import requests
import pandas as pd
from bs4 import BeautifulSoup
import streamlit as st

# -----------------------
# Settings / Constants
# -----------------------
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36"
}
DDG_HTML = "https://duckduckgo.com/html/"
DETAIL_PATTERN = re.compile(r"https?://(?:www\.)?medi-learn\.de/pruefungsprotokolle/facharztpruefung/detailed\.php\?ID=\d+", re.I)

# -----------------------
# Helpers
# -----------------------
@st.cache_data(show_spinner=False)
def ddg_site_search(query: str, max_pages: int = 5, sleep_s: float = 0.8) -> list[str]:
    """
    Scrape DuckDuckGo's HTML endpoint for query results (no API key required).
    Returns a list of result URLs (strings).
    """
    urls = set()
    next_form_data = None

    for page in range(max_pages):
        params = {"q": query}
        if next_form_data:
            # use "s" for pagination offset if provided by DDG
            params.update(next_form_data)

        resp = requests.get(DDG_HTML, params=params, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        for a in soup.select("a.result__url, a.result__a, a.result__snippet a"):
            href = a.get("href")
            if not href:
                continue
            # Unescape HTML entities
            href = html.unescape(href)
            # Only accept ML detailed pages
            m = DETAIL_PATTERN.search(href)
            if m:
                urls.add(m.group(0))

        # Find "Next" form to continue pagination (if present)
        next_btn = soup.find("a", string=re.compile(r"Next", re.I))
        if not next_btn:
            break

        # DDG html pagination can also be handled by grabbing the hidden form fields, but
        # often just increasing offset works. We'll try to read "s=" from the href.
        next_href = next_btn.get("href") or ""
        # Extract s= param
        s_match = re.search(r"[?&]s=(\d+)", next_href)
        if s_match:
            next_form_data = {"s": s_match.group(1)}
        else:
            next_form_data = None

        time.sleep(sleep_s)

    return sorted(urls)


def fetch_and_check(url: str, uni_kw: str, fach_kw: str, timeout: int = 20) -> dict | None:
    """
    Download Medi-Learn detail page and verify that both the Uni keyword and Fach keyword
    occur in the page text. Returns a record (dict) if it matches, else None.
    """
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
    except Exception as e:
        return None

    soup = BeautifulSoup(r.text, "html.parser")
    text = soup.get_text(" ", strip=True).lower()

    if uni_kw.lower() not in text or fach_kw.lower() not in text:
        return None

    # Try to extract a useful title (fallback to URL)
    title = soup.title.get_text(strip=True) if soup.title else url

    # Optional: try to grab “Atmosphäre” or “Prüfer” lines when present
    # These fields vary — we’ll do best-effort fuzzy extraction.
    def extract_line(label_patterns: list[str]) -> str | None:
        # Look for label-like text
        for lab in label_patterns:
            m = re.search(lab, text, re.I)
            if m:
                # If we matched, try to capture a short snippet following it
                idx = m.end()
                snippet = text[idx: idx + 180]
                # Stop at next label-like boundary
                snippet = re.split(r"(?:\n|  |  |•|-{2,}|={2,}|[|])", snippet)[0]
                return snippet.strip(":; \n\r\t")
        return None

    atm = extract_line([r"atmosph[aä]re\s*[:\-]?", r"stimmung\s*[:\-]?"])
    pruefer = extract_line([r"pr[üu]fer\s*[:\-]?", r"vorsitz\s*[:\-]?", r"kommission\s*[:\-]?"])

    return {
        "title": title,
        "url": url,
        "uni_match": uni_kw,
        "fach_match": fach_kw,
        "atmosphaere_guess": atm,
        "pruefer_guess": pruefer,
    }


@st.cache_data(show_spinner=False)
def collect_matches(uni: str, fach: str, max_pages: int = 5, pause_s: float = 0.6) -> pd.DataFrame:
    """
    End-to-end:
    1) Search DDG: restrict to Medi-Learn detailed.php pages + user keywords.
    2) Fetch each candidate and verify both keywords are in page text.
    3) Return DataFrame of matches.
    """
    query = f'site:medi-learn.de/pruefungsprotokolle/facharztpruefung "detailed.php?ID=" "{uni}" "{fach}"'
    candidates = ddg_site_search(query, max_pages=max_pages)

    rows = []
    for url in candidates:
        rec = fetch_and_check(url, uni_kw=uni, fach_kw=fach)
        if rec:
            rows.append(rec)
        time.sleep(pause_s)

    if not rows:
        return pd.DataFrame(columns=["title", "url", "uni_match", "fach_match", "atmosphaere_guess", "pruefer_guess"])

    # De-duplicate by ID
    def id_from_url(u: str) -> str:
        m = re.search(r"ID=(\d+)", u, re.I)
        return m.group(1) if m else u

    df = pd.DataFrame(rows)
    df["ml_id"] = df["url"].apply(id_from_url)
    df = df.drop_duplicates(subset=["ml_id"]).sort_values("ml_id", key=lambda s: s.astype(str).str.zfill(6))
    return df.reset_index(drop=True)


# -----------------------
# UI
# -----------------------
st.set_page_config(page_title="Medi-Learn Facharzt-Protokolle Zähler", page_icon="🩺", layout="wide")

st.title("🩺 Medi-Learn Facharzt-Prüfungsprotokolle – Zähler (workaround)")
st.caption("Search-based workaround that finds detail pages and verifies your filters. Deployable on Streamlit Cloud.")

with st.sidebar:
    st.header("Filter")
    uni = st.text_input("Universität (z.B. Dresden)", value="Dresden")
    fach = st.text_input("Fach (z.B. Innere Medizin)", value="Innere Medizin")

    st.header("Advanced")
    max_pages = st.slider("Max. search pages to crawl", min_value=1, max_value=10, value=5)
    st.help("If you need broader coverage, increase this — it may take a bit longer.")

    run = st.button("🔎 Search & Count")

st.markdown(
    """
**How this works:**  
This app uses DuckDuckGo’s public HTML results (no API key) to find Medi-Learn detail pages (`detailed.php?ID=…`) that mention your **Uni** and **Fach**.  
It then opens each page to verify the match and extracts small snippets (e.g., Prüfer / Atmosphäre) when possible.
"""
)

if run:
    with st.spinner("Searching and verifying results…"):
        df = collect_matches(uni=uni.strip(), fach=fach.strip(), max_pages=max_pages)

    st.subheader("Results")
    st.metric("Total matching Protokolle", value=len(df))

    if len(df):
        # Pretty table
        st.dataframe(
            df[["ml_id", "title", "url", "uni_match", "fach_match", "pruefer_guess", "atmosphaere_guess"]],
            use_container_width=True,
        )

        # Download
        csv = df.to_csv(index=False).encode("utf-8")
        st.download_button(
            "⬇️ Download CSV",
            data=csv,
            file_name=f"medi_learn_protokolle_{uni}_{fach}.csv",
            mime="text/csv",
        )
    else:
        st.info("No matching detail pages found with the current filters and search depth. "
                "Try broadening the search (increase pages) or tweak spellings (e.g., 'TU Dresden').")

st.divider()
st.markdown(
    """
### Notes & Tips
- This is a **workaround**. Medi-Learn’s on-site filters use sessions/JS; this app avoids that by site-searching public detail pages.
- Try variations like **“TU Dresden”**, **“Uniklinikum Dresden”**, or **“Innere”** if you think exact phrases differ on the pages.
- You can adapt this to other Fächer (e.g., *Neurologie*, *Chirurgie*) and other Unis.
"""
)
