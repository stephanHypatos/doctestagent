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
        <td title="Prüfer">…</td><td title="27.07.2025
