from __future__ import annotations

import re
import time
import urllib.parse
from typing import Dict, List, Optional, Tuple

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup

# =========================
# Settings
# =========================
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


# =========================
# Helpers
# =========================
def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


def absolute_url(base: str, href: Optional[str]) -> Optional[str]:
    if not href:
        return None
    return urllib.parse.urljoin(base, href)


def get_soup(session: requests.Session, url: str) -> BeautifulSoup:
    r = session.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


def form_has_filters(form: BeautifulSoup) -> bool:
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
    data: Dict[str, str] = {}
    for el in form.select("input[type=hidden]"):
        name = el.get("name")
        if name:
            data[name] = el.get("value", "") or ""
    return data


def extract_forms(session: requests.Session, url: str) -> List[dict]:
    """Return structured info for each <form> on the page."""
    soup = get_soup(session, url)
    forms = soup.find_all("form")
    structured: List[dict] = []
    for idx, frm in enumerate(forms):
        action = frm.get("action") or url
        action_url = absolute_url(url, action) or url
        selects = []
        for sel in frm.find_all("select"):
            sel_name = sel.get("name") or ""
            sel_id = sel.get("id") or ""
            # nearby label-ish text
            ctx = sel.find_parent().get_text(" ", strip=True) if sel.find_parent() else ""
            options = []
            for opt in sel.find_all("option"):
                vis = (opt.get_text(strip=True) or "").strip()
                if not vis:
                    continue
                val = opt.get("value", vis) or vis
                options.append({"visible": vis, "value": val})
            selects.append(
                {
                    "name": sel_name,
                    "id": sel_id,
                    "context": ctx,
                    "options": options,
                }
            )
        structured.append(
            {
                "index": idx,
                "action_url": action_url,
                "hidden": collect_hidden_inputs(frm),
                "selects": selects,
                "looks_like_filter": form_has_filters(frm),
                "raw_html_len": len(str(frm)),
            }
        )
    return structured


def guess_select_role(select: dict) -> Optional[str]:
    name = (select.get("name") or "").lower()
    sid = (select.get("id") or "").lower()
    around = (select.get("context") or "").lower()
    uni_tokens = ["uni", "universität", "standort", "ort", "tu dresden", "dresden"]
    fach_tokens = ["fach", "fachgebiet", "innere", "medizin", "chirurgie", "neurologie"]

    if any(t in name or t in sid for t in ["uni", "standort", "ort"]):
        return "uni"
    if any(t in name or t in sid for t in ["fach", "fachgebiet", "gebiet"]):
        return "fach"
    if any(t in around for t in uni_tokens):
        return "uni"
    if any(t in around for t in fach_tokens):
        return "fach"

    opts_norm = [norm(o["visible"]) for o in select.get("options", [])]
    city_hits = sum(
        1
        for o in opts_norm
        if any(c in o for c in ["berlin", "münchen", "hamburg", "dresden", "köln", "hannover", "frankfurt"])
    )
    fach_hits = sum(
        1
        for o in opts_norm
        if any(f in o for f in ["innere", "chirurgie", "neurologie", "anästhesie", "derma", "gyn"])
    )
    if city_hits > fach_hits:
        return "uni"
    if fach_hits > city_hits:
        return "fach"
    return None


def pick_option_value(options: List[dict], wanted_visible: str) -> Tuple[Optional[str], Optional[str]]:
    key = norm(wanted_visible)
    # exact by normalized visible
    for o in options:
        if norm(o["visible"]) == key:
            return o["value"], o["visible"]
    # fuzzy contains/startswith
    for o in options:
        k = norm(o["visible"])
        if k.startswith(key) or key in k or k in key:
            return o["value"], o["visible"]
    return None, None


def extract_result_rows_from_html(html_text: str) -> List[Dict[str, str]]:
    soup = BeautifulSoup(html_text, "html.parser")
    rows: List[Dict[str, str]] = []
    for a in soup.find_all("a", href=True):
        m = DETAIL_RE.search(a["href"])
        if not m:
            continue
        ml_id = m.group(1)
        url = absolute_url(START_URL, a["href"])
        title = a.get_text(" ", strip=True) or f"Protokoll {ml_id}"
        if url:
            rows.append({"ml_id": ml_id, "title": title, "url": url})
    # de-dup
    seen: set[str] = set()
    uniq: List[Dict[str, str]] = []
    for r in rows:
        if r["ml_id"] in seen:
            continue
        seen.add(r["ml_id"])
        uniq.append(r)
    return uniq


def find_next_page_url_from_html(html_text: str) -> Optional[str]:
    soup = BeautifulSoup(html_text, "html.parser")
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


def post_form(
    session: requests.Session,
    action_url: str,
    payload: Dict[str, str],
) -> str:
    r = session.post(action_url, data=payload, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.text


def crawl_all_pages_html(session: requests.Session, first_html: str, pause_s: float = 0.5) -> List[Dict[str, str]]:
    all_rows = extract_result_rows_from_html(first_html)
    seen_ids = {r["ml_id"] for r in all_rows}

    next_url = find_next_page_url_from_html(first_html)
    guard = 0
    while next_url and guard < 30:
        guard += 1
        time.sleep(pause_s)
        r = session.get(next_url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        html_text = r.text
        rows = extract_result_rows_from_html(html_text)
        for row in rows:
            if row["ml_id"] not in seen_ids:
                seen_ids.add(row["ml_id"])
                all_rows.append(row)
        next_url = find_next_page_url_from_html(html_text)

    all_rows.sort(key=lambda x: int(x["ml_id"]))
    return all_rows


# =========================
# UI
# =========================
st.set_page_config(
    page_title="Medi-Learn Protokolle – Form Scraper",
    page_icon="🩺",
    layout="wide",
)
st.title("🩺 Medi-Learn Facharzt-Prüfungsprotokolle — Form-Submission Scraper")
st.caption(
    "Posts the real Medi-Learn filter form (handles pagination). "
    "If auto-detection fails, use the manual mapping below."
)

with st.sidebar:
    st.header("Auto mode (tries to detect everything)")
    uni_auto = st.text_input(
        "Uni (sichtbarer Text – auto mode)",
        value="Dresden",
        help="Z. B. „Dresden“, „TU Dresden“, „Uniklinikum Dresden“",
    )
    fach_auto = st.text_input(
        "Fach (sichtbarer Text – auto mode)", value="Innere Medizin"
    )
    pause = st.slider("Pausenzeit bei Pagination (Sek.)", 0.0, 2.0, 0.5, 0.1)
    run_auto = st.button("🚀 Auto: Formular absenden & zählen")

    st.markdown("---")
    st.header("Manual mapping (use if auto fails)")
    run_discover = st.button("🔍 Discover forms & selects")

sess = requests.Session()
sess.headers.update(HEADERS)

if run_discover:
    with st.spinner("Lade Startseite & finde Formulare…"):
        forms_info = extract_forms(sess, START_URL)
    st.session_state["forms_info"] = forms_info

# Show manual mapping UI if discovery already done
forms_info = st.session_state.get("forms_info")

if forms_info:
    st.subheader("Manual mapping")
    # Choose form
    form_labels = []
    default_idx = 0
    for f in forms_info:
        label = f'Form {f["index"]} | action: {f["action_url"]} | ' \
                f'selects: {len(f["selects"])} | looks_like_filter={f["looks_like_filter"]}'
        form_labels.append(label)
        if f["looks_like_filter"]:
            default_idx = forms_info.index(f)

    form_choice = st.selectbox("Form to use", form_labels, index=default_idx)
    chosen_form = forms_info[form_labels.index(form_choice)]
    selects = chosen_form["selects"]

    if not selects:
        st.warning("No <select> found in this form. Try another form.")
    else:
        # Build select descriptors
        sel_labels = []
        for i, s in enumerate(selects):
            sample_opts = ", ".join(o["visible"] for o in s["options"][:5])
            sel_labels.append(
                f'{i}: name="{s["name"]}" id="{s["id"]}" | ex: [{sample_opts}]'
            )

        # Guess roles
        guessed_uni_idx = next(
            (i for i, s in enumerate(selects) if guess_select_role(s) == "uni"),
            0,
        )
        guessed_fach_idx = next(
            (i for i, s in enumerate(selects) if guess_select_role(s) == "fach"),
            0 if guessed_uni_idx != 0 else (1 if len(selects) > 1 else 0),
        )

        uni_sel_idx = st.selectbox("Select for UNI", list(range(len(selects))), format_func=lambda i: sel_labels[i], index=guessed_uni_idx)
        fach_sel_idx = st.selectbox("Select for FACH", list(range(len(selects))), format_func=lambda i: sel_labels[i], index=guessed_fach_idx)

        uni_options = selects[uni_sel_idx]["options"]
        fach_options = selects[fach_sel_idx]["options"]

        uni_opt_visibles = [o["visible"] for o in uni_options]
        fach_opt_visibles = [o["visible"] for o in fach_options]

        uni_choice = st.selectbox("UNI option (visible text)", uni_opt_visibles, index=0 if not uni_opt_visibles else 0)
        fach_choice = st.selectbox("FACH option (visible text)", fach_opt_visibles, index=0 if not fach_opt_visibles else 0)

        run_manual = st.button("📤 Manual: submit & count")

        if run_manual:
            try:
                payload = dict(chosen_form["hidden"])  # CSRF etc.
                # add chosen option values
                payload[selects[uni_sel_idx]["name"]] = next(
                    o["value"] for o in uni_options if o["visible"] == uni_choice
                )
                payload[selects[fach_sel_idx]["name"]] = next(
                    o["value"] for o in fach_options if o["visible"] == fach_choice
                )
                # include submit button if present
                # (scan raw form html for an input[type=submit] name/value pair)
                # simple heuristic: not strictly needed on many forms.
                first_html = post_form(sess, chosen_form["action_url"], payload)
                all_rows = crawl_all_pages_html(sess, first_html, pause_s=pause)

                st.subheader("Ergebnis (Manual)")
                st.metric("Anzahl Protokolle", len(all_rows))
                if all_rows:
                    df = pd.DataFrame(all_rows)
                    base_cols = ["ml_id", "title", "url"]
                    df = df[base_cols + [c for c in df.columns if c not in base_cols]]
                    st.dataframe(df, use_container_width=True)
                    csv = df.to_csv(index=False).encode("utf-8")
                    st.download_button(
                        "⬇️ CSV herunterladen",
                        data=csv,
                        file_name="medi_learn_protokolle_manual.csv",
                        mime="text/csv",
                    )
                else:
                    st.info("Keine Treffer. Prüfe gewählte Optionen/Formular.")
            except Exception as e:
                st.error(f"Fehler (Manual): {e}")

# Auto mode: keep original behavior
if run_auto:
    try:
        with st.spinner("Auto: lade Startseite & detektiere Formular…"):
            start_html = sess.get(START_URL, headers=HEADERS, timeout=30).text
        # Build forms info from HTML we already have
        soup = BeautifulSoup(start_html, "html.parser")
        forms = soup.find_all("form")
        target_form = None
        for frm in forms:
            if form_has_filters(frm):
                target_form = frm
                break
        if not target_form:
            target_form = forms[0] if forms else None
        if not target_form:
            raise RuntimeError("No <form> found on start page.")

        action = target_form.get("action") or START_URL
        action_url = absolute_url(START_URL, action) or START_URL
        payload = collect_hidden_inputs(target_form)

        # try to auto map selects
        selects_bs = list(target_form.find_all("select"))
        uni_name = fach_name = None
        uni_val = fach_val = None

        def pick_from_bs(sel, wanted):
            opts = []
            for o in sel.find_all("option"):
                vis = (o.get_text(strip=True) or "").strip()
                if not vis:
                    continue
                val = o.get("value", vis) or vis
                opts.append({"visible": vis, "value": val})
            return pick_option_value(opts, wanted)

        # role-based first
        for sel in selects_bs:
            sel_name = sel.get("name") or ""
            sel_id = sel.get("id") or ""
            ctx = sel.find_parent().get_text(" ", strip=True) if sel.find_parent() else ""
            role = guess_select_role(
                {
                    "name": sel_name,
                    "id": sel_id,
                    "context": ctx,
                    "options": [
                        {"visible": (o.get_text(strip=True) or "").strip(), "value": o.get("value", "") or ""}
                        for o in sel.find_all("option")
                    ],
                }
            )
            if role == "uni" and uni_val is None:
                v, _ = pick_from_bs(sel, uni_auto)
                if v:
                    uni_val = v
                    uni_name = sel.get("name")
            if role == "fach" and fach_val is None:
                v, _ = pick_from_bs(sel, fach_auto)
                if v:
                    fach_val = v
                    fach_name = sel.get("name")

        # fallback: any select containing target visible text
        if not uni_val:
            for sel in selects_bs:
                v, _ = pick_from_bs(sel, uni_auto)
                if v:
                    uni_val = v
                    uni_name = sel.get("name")
                    break
        if not fach_val:
            for sel in selects_bs:
                v, _ = pick_from_bs(sel, fach_auto)
                if v:
                    fach_val = v
                    fach_name = sel.get("name")
                    break

        if not uni_name or not fach_name or not uni_val or not fach_val:
            raise RuntimeError(
                "Could not map both filters. "
                f"Resolved → uni: name={uni_name} val={uni_val}, "
                f"fach: name={fach_name} val={fach_val}"
            )

        payload[uni_name] = uni_val
        payload[fach_name] = fach_val

        first_html = post_form(sess, action_url, payload)
        all_rows = crawl_all_pages_html(sess, first_html, pause_s=pause)

        st.subheader("Ergebnis (Auto)")
        st.metric("Anzahl Protokolle", len(all_rows))
        if all_rows:
            df = pd.DataFrame(all_rows)
            base_cols = ["ml_id", "title", "url"]
            df = df[base_cols + [c for c in df.columns if c not in base_cols]]
            st.dataframe(df, use_container_width=True)
            csv = df.to_csv(index=False).encode("utf-8")
            st.download_button(
                "⬇️ CSV herunterladen",
                data=csv,
                file_name="medi_learn_protokolle_auto.csv",
                mime="text/csv",
            )
        else:
            st.info(
                "Keine Treffer in Auto-Modus. Klicken Sie links auf "
                "„Discover forms & selects“ und nutzen Sie die manuelle Zuordnung."
            )
    except Exception as exc:
        st.error(f"Fehler (Auto): {exc}")
