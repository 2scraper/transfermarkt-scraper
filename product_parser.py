"""
product_parser.py
------------------
Transfermarkt extraction. Kept under the family's usual filename for
consistency with the sibling repos' tooling and mental model, even though
what this file reads is people and clubs rather than products — see
output_writer.py's docstring for why the row classes reflect that and this
file's name does not.

No JSON-LD anywhere on this site
---------------------------------
Checked on every page kind this repo covers — a market-value ranking page,
a club squad page, a transfer list, and a player profile — zero
`<script type="application/ld+json">` blocks on any of them, and no
OpenGraph beyond a title/image. So there is no "first count the JSON-LD
blocks" primary path here the way the e-commerce members of this family
have one; every field in this file comes from the DOM, anchored on the
site's own stable hooks where one exists:

  * `/profil/spieler/{id}`, `/verein/{id}` and (on the transfers listing)
    `/jumplist/transfers/spieler/{id}/transfer_id/{id}` are URL PATTERNS —
    a contract with search engines, per the family rule, and the anchor
    used everywhere an id is needed.
  * A player's profile header carries real `schema.org/Person` MICRODATA
    (`itemprop="birthDate"`, `"nationality"`, `"height"`, `"affiliation"`)
    — not JSON-LD, but still the site's own structured markup rather than a
    CSS class, and read directly for those fields.
  * Everywhere else (listing tables, the rest of the profile header) the
    anchor is the site's own BEM-ish class names (`data-header__label`,
    `posrela`, `hauptlink`) or table column POSITION, both measured against
    real captures rather than assumed.

Money
-----
Every value on this site is in EUR in every locale this repo supports —
checked directly: the SAME player's SAME market value prints in all five,
never converted, only reformatted (`€220.00m` / `220,00 Mio. €` / etc.) — so
there is no multi-currency guessing to do here, unlike this family's
e-commerce members. `parse_market_value(text, locale=...)` (see that
function) picks the decimal separator, symbol position and suffix
vocabulary per locale from `_MONEY_FORMATS`, measured 2026-09-16 across a
ranking page in each locale (see that dict's own comment for the exact
figures and which suffixes are live-measured vs. documented-only).

A value of exactly "-" means no market value is published (common for a
free agent or a very old veteran) and must parse to `None`, not to zero: a
squad total that trusted a naive number-or-zero read would understate every
such player's contribution as -100% rather than "unknown".

Locales
-------
v0.1 of this repo verified only the English `.com` site. v0.3 adds four more
(`de`, `.world`=ru, `.co.kr`=ko, `.jp`) — see `LOCALE_BY_HOST` for exactly
which hosts, and its comment for what was checked before each was added.
Structurally these five are identical (same classes, same URL/id scheme);
what differs is TEXT, normalised into a single English vocabulary per
CLAUDE.md's convention for this family:

  * money — `parse_market_value(text, locale=...)`, above.
  * position / nationality — `translate_position` / `translate_nationality`,
    each backed by a per-locale {native: english} table built from real
    captures, with graceful untranslated pass-through (+ a logged warning)
    for anything not in that table — never a guessed translation.
  * player-detail label lookups (joined/contract-expires/position/full-name/
    place-of-birth/last-update prefix) — `_label`/`_info`, keyed by
    `_HEADER_LABEL_KEYS`/`_INFO_LABEL_KEYS`/`_LAST_UPDATE_PREFIX`.
  * dates — `normalize_date(text, locale=...)`, to ISO 8601 (`YYYY-MM-DD`).

Selecting a locale is purely a matter of which host's URL is passed in —
same convention as this family's `mediamarkt-scraper` (no separate
`--locale`/`--country` flag; `--url https://www.transfermarkt.de/...` is
the whole mechanism). Every row-constructing function derives both the
locale (`locale_of(base_url)`) and the `source` field (`site_host(base_url)`)
from that URL rather than hardcoding the English host, so a `.de` fetch
correctly reports `source="www.transfermarkt.de"`, not `"transfermarkt.com"`.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from output_writer import Player, Transfer

# "Fail loudly" (CLAUDE.md §8): a parser returning [] or None on a markup
# change it didn't expect must say so, not just report success with fewer
# rows. Every list parser below logs the table-not-found case and a summary
# of any row it had to skip; parse_player_detail logs the header-not-found
# case. None of this changes what is returned -- it only makes a silent
# empty/thin result loud enough to notice in a log a live run actually
# produces.
logger = logging.getLogger("product_parser")

# ---------------------------------------------------------------------------
# Hosts / locales
# ---------------------------------------------------------------------------
# Transfermarkt runs the same platform under many country TLDs
# (transfermarkt.de, .world, .co.kr, .jp, .es, ...), the way MediaMarkt runs
# one platform across ten. Per CLAUDE.md §5 (the "mediamarkt.lu lesson": a
# same-brand, hreflang-listed host that turned out to run on a different
# platform entirely) a locale is never assumed to behave identically just
# because it shares the brand — each one below was fetched live (2026-09-16)
# and its DOM checked against the English site before being added here.
#
# Verified identical across all five: `table.items` structure, `posrela`/
# `hauptlink`/`flaggenrahmen`/`inline-table` class names, and the
# `/profil/spieler/{id}` + `/verein/{id}` URL/id scheme (the `--slug` segment
# is never transliterated, even into Cyrillic/Hangul/Kanji). So NONE of the
# row-extraction selectors in this file need to change per locale — only the
# TEXT they read out (money strings, position names, nationality names, and
# the player-detail label vocabulary) differs, and that is what
# `LOCALE_BY_HOST` and the translation tables below exist to handle.
#
# The other ~20 hosts transfermarkt.com's own hreflang set lists (`.es`,
# `.co.uk`, `.fr`, ...) are NOT in this set: nobody has fetched and compared
# their markup yet, so — same rule as mediamarkt-scraper — they are refused
# with a reason rather than silently attempted as if they were one of the
# five actually checked here. Add a host only after it has been fetched and
# its structure/vocabulary confirmed, never by analogy with a sibling TLD.
LOCALE_BY_HOST = {
    "transfermarkt.com": "en", "www.transfermarkt.com": "en",
    "transfermarkt.de": "de", "www.transfermarkt.de": "de",
    "transfermarkt.world": "ru", "www.transfermarkt.world": "ru",
    "transfermarkt.co.kr": "ko", "www.transfermarkt.co.kr": "ko",
    "transfermarkt.jp": "ja", "www.transfermarkt.jp": "ja",
}
HOSTS = set(LOCALE_BY_HOST)

DEFAULT_LOCALE = "en"


def site_host(url: str) -> str:
    try:
        return urlparse(url).hostname or ""
    except ValueError:
        return ""


def locale_of(url: str) -> str:
    """The locale code for a host, defaulting to `en` for an unrecognised or
    empty host — used only after `is_supported_host` has already accepted
    the URL, so this default is a fallback for a bare id/slug helper URL
    (see `player_url`/`club_squad_url`, both built against `BASE`), never a
    silent guess about a locale that was not actually verified."""
    return LOCALE_BY_HOST.get(site_host(url), DEFAULT_LOCALE)


def unsupported_reason(url: str) -> Optional[str]:
    host = site_host(url)
    if not host:
        return "is not a valid URL"
    if host in HOSTS:
        return None
    if "transfermarkt" in host:
        return (f"is a Transfermarkt locale site, but only {sorted(HOSTS)} "
                f"are verified in this repo (v0.3) — its markup was not "
                f"checked and may differ (see CLAUDE.md's mediamarkt.lu "
                f"lesson: a same-brand host is not proof of the same "
                f"platform)")
    return "is not a Transfermarkt site"


def is_supported_host(url: str) -> bool:
    return unsupported_reason(url) is None


# ---------------------------------------------------------------------------
# Canonical URLs
# ---------------------------------------------------------------------------
# The `--slug` segment of every Transfermarkt URL is cosmetic — verified
# 2026-09-10: a squad page fetched with the slug replaced by the single
# letter "x" returned the identical page as the real slug. So a caller that
# only has an id (no slug) can still build a working URL.
BASE = "https://www.transfermarkt.com"

MARKET_VALUES_URL = f"{BASE}/spieler-statistik/wertvollstespieler/marktwertetop"
TRANSFERS_URL = f"{BASE}/statistik/neuestetransfers"


def player_url(player_id: str, slug: str = "x") -> str:
    return f"{BASE}/{slug}/profil/spieler/{player_id}"


def club_squad_url(club_id: str, season: Optional[str] = None, slug: str = "x") -> str:
    url = f"{BASE}/{slug}/kader/verein/{club_id}"
    if season:
        url += f"/saison_id/{season}"
    return url


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------
# Only `transfers` uses this: a plain, independently-addressable `?page=N`,
# measured 2026-09-10 (page 2 shared 0 transfer ids with page 1, 25 rows
# each). `market-values`'s pagination is NOT this shape — see page_flow.py's
# "gallery pagination trap" — and never routes through this function.
PAGE_PARAM = "page"


def page_url(url: str, page_num: int) -> str:
    from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    if page_num <= 1:
        query.pop(PAGE_PARAM, None)
    else:
        query[PAGE_PARAM] = str(page_num)
    return urlunsplit(parts._replace(query=urlencode(query)))


def page_number_from_url(url: str) -> Optional[int]:
    from urllib.parse import urlsplit, parse_qsl
    query = dict(parse_qsl(urlsplit(url).query))
    if PAGE_PARAM not in query:
        return 1
    try:
        return int(query[PAGE_PARAM])
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Ids from URLs
# ---------------------------------------------------------------------------
_PLAYER_ID_RE = re.compile(r"/profil/spieler/(\d+)")
_CLUB_ID_RE = re.compile(r"/verein/(\d+)")
_TRANSFER_ID_RE = re.compile(r"/transfer_id/(\d+)")
_LEAGUE_ID_RE = re.compile(r"/wettbewerb/([A-Za-z0-9]+)")


def player_id_from_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    m = _PLAYER_ID_RE.search(url)
    return m.group(1) if m else None


def club_id_from_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    m = _CLUB_ID_RE.search(url)
    return m.group(1) if m else None


def transfer_id_from_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    m = _TRANSFER_ID_RE.search(url)
    return m.group(1) if m else None


def league_id_from_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    m = _LEAGUE_ID_RE.search(url)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------
# Every locale prints the SAME value in the SAME currency (EUR) — checked
# across all five (a shared player's market value matches, converted only in
# format, never in number) — so there is still no multi-currency guessing to
# do here, unlike this family's e-commerce members. What DOES differ per
# locale, measured 2026-09-16 on the market-values ranking page:
#
#   locale  symbol      decimal   thousand suffix   million suffix
#   en      prefix "€"  period    k (measured page1 has none < 1m; documented)
#   de      suffix " €" comma     Tsd. (documented, not measured)   Mio.
#   ru      suffix " €" comma     тыс. (documented, not measured)   млн
#   ko      suffix " €" period    th. (documented, not measured)    mil.
#   ja      suffix " €" period    k (documented, not measured)      m
#
# billion (`bn`/`Mrd.`/`млрд`/`bil.`/`bn`) is documented-but-never-observed
# in EVERY locale including English (this repo's pre-existing precedent —
# see the original en-only docstring this replaced) since it only appears in
# an aggregate squad-value table this repo does not parse; kept for all
# locales on the same "costs nothing to accept" basis.
_MONEY_FORMATS = {
    "en": {"symbol_prefix": True, "decimal": ".",
           "suffixes": {"bn": 1_000_000_000, "m": 1_000_000, "k": 1_000}},
    "de": {"symbol_prefix": False, "decimal": ",",
           "suffixes": {"mrd.": 1_000_000_000, "mio.": 1_000_000, "tsd.": 1_000}},
    "ru": {"symbol_prefix": False, "decimal": ",",
           "suffixes": {"млрд": 1_000_000_000, "млн": 1_000_000, "тыс.": 1_000,
                        "тыс": 1_000}},
    "ko": {"symbol_prefix": False, "decimal": ".",
           "suffixes": {"bil.": 1_000_000_000, "mil.": 1_000_000, "th.": 1_000}},
    "ja": {"symbol_prefix": False, "decimal": ".",
           "suffixes": {"bn": 1_000_000_000, "m": 1_000_000, "k": 1_000}},
}

# One regex per locale: suffixes sorted longest-first so e.g. German "Mrd."
# is never read as "Mio." with a dangling "rd." (the same "longest suffix
# first" reasoning the original English-only regex already used for
# "bn"/"m"). `re.escape` because "тыс." and "Mio." both contain a literal
# dot. Amount group is digits + one optional decimal-separator run — the
# separator itself is normalised in `parse_market_value`, not here.
def _build_money_re(fmt: dict) -> "re.Pattern":
    suffixes = sorted(fmt["suffixes"], key=len, reverse=True)
    suffix_alt = "|".join(re.escape(s) for s in suffixes)
    if fmt["symbol_prefix"]:
        pattern = rf"€\s*([\d]+(?:[.,]\d+)?)\s*({suffix_alt})?"
    else:
        pattern = rf"([\d]+(?:[.,]\d+)?)\s*({suffix_alt})?\s*€"
    return re.compile(pattern, re.IGNORECASE)


_MONEY_RE_BY_LOCALE = {loc: _build_money_re(fmt) for loc, fmt in _MONEY_FORMATS.items()}


def parse_market_value(
    text: Optional[str], locale: str = DEFAULT_LOCALE
) -> Tuple[Optional[float], Optional[str]]:
    """(amount_in_eur, currency) from a Transfermarkt money string.

    Returns (None, None) for "-" (no value published) or unparseable text.
    Currency is always "EUR" when an amount was found — see the module
    docstring for why there is nothing to disambiguate here.

    `locale` selects the decimal separator, symbol position and suffix
    vocabulary (see `_MONEY_FORMATS`); it defaults to "en" so every existing
    call site that has not been updated to pass one keeps its old, verified
    behaviour rather than silently changing shape.
    """
    if not text:
        return None, None
    text = text.strip()
    if text in ("-", "", "?"):
        return None, None
    fmt = _MONEY_FORMATS.get(locale, _MONEY_FORMATS[DEFAULT_LOCALE])
    money_re = _MONEY_RE_BY_LOCALE.get(locale, _MONEY_RE_BY_LOCALE[DEFAULT_LOCALE])
    m = money_re.search(text)
    if not m:
        return None, None
    raw, suffix = m.groups()
    if fmt["decimal"] == ",":
        # A comma is the DECIMAL point in this locale ("220,00 Mio."), the
        # opposite of English's thousands-grouping comma — swap it for a
        # period rather than stripping it, then drop any leftover period
        # (a thousands grouping in THIS locale) before parsing as float.
        raw = raw.replace(".", "").replace(",", ".")
    else:
        # Locale's decimal point is a period; a comma here is a thousands
        # grouping on a suffixed figure, so only strip it.
        raw = raw.replace(",", "")
    try:
        amount = float(raw)
    except ValueError:
        return None, None
    if suffix:
        amount *= fmt["suffixes"][suffix.lower()]
    return amount, "EUR"


# ---------------------------------------------------------------------------
# Locale text normalisation (positions, nationalities, labels)
# ---------------------------------------------------------------------------
# Per CLAUDE.md's convention for this family: normalise locale-specific text
# into a single English vocabulary rather than shipping five different sets
# of position/nationality strings for one underlying fact. Every mapping
# below was measured, not guessed — extracted from real captures (2026-09-16)
# by matching the SAME real player's row across a locale and the English
# site (a market-values top-25 shared across all five locales, plus a full
# Manchester City squad page shared between en/de) and diffing the text at
# the identical DOM position. A pair built from mismatched snapshots (see
# the next paragraph) was caught and thrown out, not trusted.
#
# Nationality pairs come from two methods, and they agree, which is why
# both are recorded rather than just the better one.
#
# The first was defensive. Two players' flag lists differed between the
# English squad capture (2026-09-12) and the German one (2026-09-16) — real
# data drift on the site between the two fetches, not a parsing bug (same
# `td` index, same selector, confirmed by hand). A dual-nationality row read
# across two captures taken days apart can therefore pair the wrong names,
# so that method used ONLY players carrying exactly one flag in both, where
# order cannot be ambiguous. Safe, but it left the tables thin.
#
# The second removes that restriction entirely, using the site against
# itself: the JAPANESE locale prints nationality names in ENGLISH (measured
# — see the note on `_NATIONALITY_EN["ja"]` below), in the same DOM order as
# every other locale. So `ja` is a same-page English reference, and
# `zip(de_flags, ja_flags)` pairs a dual-nationality row exactly, with no
# cross-capture drift to worry about because both come from the same fetch
# batch. Checked before use: on the 25 players shared across all five
# locales, the `ja` list equalled the `en` list 25 times out of 25.
#
# Where both methods produced a pair they agreed on every one, so the tables
# below are their union. Re-derive by diffing the captures at the identical
# DOM position; do not extend these by translating from memory.
#
# Coverage is intentionally uneven per locale — German has the most (10
# positions, from a full 43-player squad page) because that capture was
# richer than the market-values top-25 (7-8 positions, attacker-heavy) run
# for de/ru/ko/ja. An entry not in a locale's table is passed through
# UNTRANSLATED with a logged warning (see `translate_position` /
# `translate_nationality`) rather than guessed by analogy with a sibling
# locale — the same "never silently mis-scrape" principle `unsupported_reason`
# already applies to hosts.
_POSITION_EN = {
    "de": {
        "Torwart": "Goalkeeper",
        "Innenverteidiger": "Centre-Back",
        "Linker Verteidiger": "Left-Back",
        "Rechter Verteidiger": "Right-Back",
        "Defensives Mittelfeld": "Defensive Midfield",
        "Zentrales Mittelfeld": "Central Midfield",
        "Offensives Mittelfeld": "Attacking Midfield",
        "Linksaußen": "Left Winger",
        "Rechtsaußen": "Right Winger",
        "Mittelstürmer": "Centre-Forward",
    },
    "ru": {
        "Центр. защитник": "Centre-Back",
        "Опорный полузащитник": "Defensive Midfield",
        "Центр. полузащитник": "Central Midfield",
        "Атак. полузащитник": "Attacking Midfield",
        "Левый Вингер": "Left Winger",
        "Правый Вингер": "Right Winger",
        "Центральный нап.": "Centre-Forward",
    },
    "ko": {
        "중앙 수비수": "Centre-Back",
        "수비형 미드필더": "Defensive Midfield",
        "중앙 미드필더": "Central Midfield",
        "공격형 미드필더": "Attacking Midfield",
        "좌측 윙 포워드": "Left Winger",
        "우측 윙 포워드": "Right Winger",
        "중앙 공격수": "Centre-Forward",
    },
    "ja": {
        "センターバック": "Centre-Back",
        "守備的ミッドフィールダー": "Defensive Midfield",
        "セントラルミッドフィールダー": "Central Midfield",
        "攻撃的ミッドフィールダー": "Attacking Midfield",
        "左ウィンガー": "Left Winger",
        "右ウィンガー": "Right Winger",
        "センターフォワード": "Centre-Forward",
    },
}

# The player-detail "Position" info-table row prefixes the specific position
# with a broad unit ("Attack - Centre-Forward", measured on the English
# Haaland/Yamal captures) — each locale's own word for that same prefix,
# measured on the same Haaland profile (2026-09-16): German "Sturm",
# Russian "Нападающий", Korean "공격", Japanese "フォワード". Stripped before
# the lookup above rather than added as separate dictionary keys, so the
# tables stay the single source of truth for the position name itself.
_POSITION_PREFIX_SEP = {
    "de": " - ", "ru": " - ", "ko": " - ", "ja": " - ", "en": " - ",
}


def translate_position(text: Optional[str], locale: str = DEFAULT_LOCALE) -> Optional[str]:
    """`text` normalised to its English position name, or `text` unchanged
    (with a logged warning) when `locale` has no entry for it — e.g. a
    locale's Goalkeeper/Left-Back/Right-Back before those are measured for
    it, or a position this repo has simply never seen. Never raises, never
    fabricates a translation."""
    if not text:
        return text
    # The info-table's "Attack - Centre-Forward"-shaped value: only the part
    # after the last separator is a position name the tables key on.
    sep = _POSITION_PREFIX_SEP.get(locale, " - ")
    lookup = text.rsplit(sep, 1)[-1].strip() if sep in text else text
    table = _POSITION_EN.get(locale)
    if not table or locale == DEFAULT_LOCALE:
        return text
    translated = table.get(lookup)
    if translated is None:
        logger.warning("no %s->en position translation for %r -- passing "
                       "through untranslated.", locale, text)
        return text
    return translated


_NATIONALITY_EN = {
    "de": {
        "Algerien": "Algeria", "Antigua und Barbuda": "Antigua and Barbuda",
        "Argentinien": "Argentina", "Barbados": "Barbados", "Belgien": "Belgium",
        "Brasilien": "Brazil", "DR Kongo": "DR Congo", "Deutschland": "Germany",
        "Ecuador": "Ecuador", "Elfenbeinküste": "Cote d'Ivoire", "England": "England",
        "Frankreich": "France", "Gambia": "The Gambia", "Georgien": "Georgia",
        "Ghana": "Ghana", "Irland": "Ireland",
        "Italien": "Italy", "Jamaika": "Jamaica", "Kamerun": "Cameroon",
        "Kanada": "Canada", "Kroatien": "Croatia", "Litauen": "Lithuania",
        "Niederlande": "Netherlands", "Nigeria": "Nigeria", "Norwegen": "Norway",
        "Portugal": "Portugal", "Schottland": "Scotland", "Schweiz": "Switzerland",
        "Spanien": "Spain", "St. Kitts und Nevis": "St. Kitts & Nevis",
        "Ungarn": "Hungary", "Usbekistan": "Uzbekistan",
    },
    "ru": {
        "Аргентина": "Argentina", "Англия": "England", "Бразилия": "Brazil",
        "Венгрия": "Hungary", "Германия": "Germany", "Грузия": "Georgia",
        "Ирландия": "Ireland", "Испания": "Spain", "Италия": "Italy",
        "Камерун": "Cameroon",
        "Кот-д'Ивуар": "Cote d'Ivoire", "Нигерия": "Nigeria", "Норвегия": "Norway",
        "Португалия": "Portugal", "Сент-Китс и Невис": "St. Kitts & Nevis",
        "Франция": "France", "Шотландия": "Scotland", "Эквадор": "Ecuador",
        "Ямайка": "Jamaica",
    },
    "ko": {
        "아르헨티나": "Argentina", "잉글랜드": "England", "브라질": "Brazil",
        "헝가리": "Hungary", "독일": "Germany", "조지아": "Georgia",
        "아일랜드": "Ireland", "스페인": "Spain", "이탈리아": "Italy",
        "카메룬": "Cameroon",
        "코트디부아르": "Cote d'Ivoire", "나이지리아": "Nigeria", "노르웨이": "Norway",
        "포르투갈": "Portugal", "세인트키츠 네비스": "St. Kitts & Nevis",
        "프랑스": "France", "스코틀랜드": "Scotland", "에콰도르": "Ecuador",
        "자메이카": "Jamaica",
    },
    # Japanese is the one locale measured to leave nationality names in
    # English (flag `title`/`alt` text is plain English, e.g. "Norway", even
    # though JP club names elsewhere on the same page ARE localised — a real
    # asymmetry, not a scraping gap) — so there is nothing to translate here.
    # `translate_nationality` below passes Japanese text through unchanged
    # for this reason, not because it was skipped.
}


def translate_nationality(text: Optional[str], locale: str = DEFAULT_LOCALE) -> Optional[str]:
    """`text` normalised to its English nationality name, or `text`
    unchanged (with a logged warning) when `locale` has no entry for it.
    Japanese always passes through unchanged — see `_NATIONALITY_EN`'s
    docstring note: that locale's own flag text is already English."""
    if not text or locale in (DEFAULT_LOCALE, "ja"):
        return text
    table = _NATIONALITY_EN.get(locale)
    if not table:
        return text
    translated = table.get(text)
    if translated is None:
        logger.warning("no %s->en nationality translation for %r -- passing "
                       "through untranslated.", locale, text)
        return text
    return translated


# ---------------------------------------------------------------------------
# Player-detail label vocabulary (per locale)
# ---------------------------------------------------------------------------
# `_label_map`/`_info_table_map` build a GENERIC {label: value} dict from
# whatever text is actually on the page — that mechanism itself needs no
# locale changes (see those functions' docstrings). What DOES need a
# locale-keyed lookup is the caller in `parse_player_detail`, which reads
# specific English label strings out of that dict. Every key below is the
# LOWERCASED label text measured on Erling Haaland's own profile (id
# 418560) fetched in de/ru/ko/ja on 2026-09-16, matched against the same
# semantic row on the pre-existing English capture (Lamine Yamal / Haaland,
# 2026-09-10) — same field, same position in the header/info-table, text
# diffed directly, not translated from memory.
_HEADER_LABEL_KEYS = {
    # semantic key -> {locale: native label key, as produced by _label_map}
    "position": {"de": "position", "ru": "амплуа", "ko": "위치", "ja": "ポジション"},
    "joined": {"de": "im team seit", "ru": "в команде с", "ko": "가입", "ja": "加入日"},
    "contract expires": {"de": "vertrag bis", "ru": "контракт до",
                         "ko": "계약 기간", "ja": "契約満了"},
}

_INFO_LABEL_KEYS = {
    # Every non-English capture available (Haaland, all four locales) shows
    # the "name in home country" row, never the "Full name" one -- Yamal
    # (the only "Full name" example this repo has) was only ever captured in
    # English. So the locale key for "full name" itself is NOT guessed here
    # by analogy with "name in home country"'s pattern: it is simply absent,
    # which makes `_info()` fall back to the (English) semantic key -- a
    # correct no-op on these captures, and a logged pass-through rather than
    # a fabricated label if a "Full name"-shaped non-English profile ever is
    # captured.
    "name in home country": {"de": "name im heimatland", "ru": "имя на родине",
                             "ko": "본국 이름 (한글 성명)", "ja": "母国語表記"},
    "place of birth": {"de": "geburtsort", "ru": "место рождения",
                       "ko": "출생지", "ja": "出生地"},
}

# The market-value box's "Last update: <date>" prefix, per locale -- stripped
# the same way the English `.replace("Last update:", "")` already did.
_LAST_UPDATE_PREFIX = {
    "en": "Last update:",
    "de": "Letzte Änderung:",
    "ru": "Последнее изменение:",
    "ko": "마지막 업데이트:",
    "ja": "最新アップデート:",
}


def _label(labels: Dict[str, str], semantic_key: str, locale: str) -> Optional[str]:
    """`labels.get(...)` through the locale's own key for `semantic_key`,
    falling back to the English key so an `en`-locale call (or a locale
    missing this particular row) behaves exactly as before."""
    native_key = _HEADER_LABEL_KEYS.get(semantic_key, {}).get(locale)
    if native_key and native_key in labels:
        return labels[native_key]
    return labels.get(semantic_key)


def _info(info: Dict[str, str], semantic_key: str, locale: str) -> Optional[str]:
    native_key = _INFO_LABEL_KEYS.get(semantic_key, {}).get(locale)
    if native_key and native_key in info:
        return info[native_key]
    return info.get(semantic_key)


# ---------------------------------------------------------------------------
# Date normalisation
# ---------------------------------------------------------------------------
# Measured 2026-09-16 on Haaland's own profile (id 418560) across all five
# locales -- the SAME date ("21 July 2000" / "1 July 2022" / etc.) printed
# in a different field ORDER, not just a different separator:
#
#   en  21/07/2000            day/month/year, slash
#   de  21.07.2000            day.month.year, period
#   ru  21 июля 2000 г.       day <spelled-out month, genitive case> year "г."
#   ko  2000년 7월 21일         year<年>month<月>day<日>
#   ja  2000/07/21            year/month/day, slash
#
# Normalised to ISO 8601 (`YYYY-MM-DD`) -- a single, sortable, locale-free
# vocabulary for a date, the same spirit as the position/nationality tables
# above. Returns the ORIGINAL text unchanged (with a logged warning) rather
# than raising or guessing when a string does not match the one shape
# measured for its locale -- e.g. a truly unparseable value should surface as
# "still the original site text", never as a fabricated date or a crash that
# takes the whole row down with it.
_RU_MONTHS_GENITIVE = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11,
    "декабря": 12,
}
_DATE_RE_SLASH_DMY = re.compile(r"(\d{1,2})[/.](\d{1,2})[/.](\d{4})")
_DATE_RE_SLASH_YMD = re.compile(r"(\d{4})[/.](\d{1,2})[/.](\d{1,2})")
_DATE_RE_RU = re.compile(r"(\d{1,2})\s+([а-яА-Я]+)\s+(\d{4})")
_DATE_RE_KO = re.compile(r"(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일")


def normalize_date(text: Optional[str], locale: str = DEFAULT_LOCALE) -> Optional[str]:
    """`text`'s date reduced to `YYYY-MM-DD`, or `text` unchanged if it does
    not match the one shape measured for `locale` (see module docstring
    above for the five shapes)."""
    if not text:
        return text
    text = text.strip()
    try:
        if locale == "ko":
            m = _DATE_RE_KO.search(text)
            if m:
                y, mo, d = m.groups()
                return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
        elif locale == "ru":
            m = _DATE_RE_RU.search(text)
            if m:
                d, month_word, y = m.groups()
                mo = _RU_MONTHS_GENITIVE.get(month_word.lower())
                if mo:
                    return f"{int(y):04d}-{mo:02d}-{int(d):02d}"
        elif locale == "ja":
            m = _DATE_RE_SLASH_YMD.search(text)
            if m:
                y, mo, d = m.groups()
                return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
        else:
            # en/de: day/month/year (slash or period separator).
            m = _DATE_RE_SLASH_DMY.search(text)
            if m:
                d, mo, y = m.groups()
                return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
    except ValueError:
        pass
    logger.warning("no %s date shape matched for %r -- passing through "
                   "unchanged.", locale, text)
    return text


# ---------------------------------------------------------------------------
# Bot / block detection
# ---------------------------------------------------------------------------
# AWS WAF is the one this site actually serves, and it led this set for
# exactly that reason. Measured 2026-09-16: www.transfermarkt.com sits behind
# AWS WAF on CloudFront and answers a plain datacentre-IP request with a
# CAPTCHA action — HTTP 405, `x-amzn-waf-action: captcha`, `server:
# CloudFront`, a 2331-byte "Human Verification" body — on 8 of 10 consecutive
# requests. A served page answers from `server: nginx`, the site's own origin.
#
# Until v0.4.0 this set covered six vendors, NONE of which appears on this
# site, and omitted the only one that does. The comment below used to say a
# detector that never fires is cheap insurance; the real cost was the
# opposite one it also warned about — "a narrow list is how a real challenge
# gets reported as an empty page months later" — which is precisely what
# happened here.
#
# Each marker below was counted on 17 known-good captures on disk (both
# listing kinds, a squad, a transfer list and player profiles across five
# locales): 0 occurrences of every one, per §18's "count it on a page you
# know is good". A bare `awswaf` substring and the phrase "Human
# Verification" also scored 0 but are deliberately NOT used: the first also
# matches 2Captcha's own auto-solve extension, which injects
# `content/captcha/amazon_waf/interceptor.js` into every page the Scraping
# Browser loads (the `cf-turnstile` trap of §19, one vendor over), and the
# second is ordinary English that a real page could carry.
BOT_CHALLENGE_MARKERS = {
    "AWS WAF": ("captcha.awswaf.com", "token.awswaf.com",
                "window.gokuProps", "awsWafCookieDomainList"),
    "Cloudflare": ("Attention Required! | Cloudflare", "cf-error-details",
                  "cf-turnstile", "Just a moment...", "cf-chl-"),
    "DataDome": ("datadome", "geo.captcha-delivery.com"),
    "PerimeterX/HUMAN": ("px-captcha", "_px3", "perimeterx"),
    "reCAPTCHA": ("recaptcha/api.js", "g-recaptcha"),
    "hCaptcha": ("hcaptcha.com/captcha",),
    "generic": ("Access Denied", "Request unsuccessful", "sorry, you have been blocked"),
}

# Which vendors mean "a challenge we can pay to clear" (state `captcha`,
# whose policy has solve=True) rather than "an edge-level refusal" (state
# `blocked`, solve=False). AWS WAF belongs here because 2Captcha solves it:
# task type `amazon_waf`, confirmed available against the live product on
# 2026-09-16, taking sitekey/iv/context/url — all four of which the challenge
# page hands over in `window.gokuProps`. See captcha_solver.solve_amazon_waf.
SOLVABLE_VENDORS = ("AWS WAF", "reCAPTCHA", "hCaptcha", "Cloudflare")

# AWS WAF names its own action in a response header and even allowlists it
# for cross-origin reads (`access-control-expose-headers: x-amzn-waf-action`).
# That is the front telling us outright what it did — a stronger signal than
# any text match, so `detect_page_state` consults it first.
WAF_ACTION_HEADER = "x-amzn-waf-action"

# Scraping Browser API auto-solve extension: seen in this family injecting
# its own hunter scripts into every page it loads, which then match a
# challenge-vendor marker on an otherwise-clean page (mediamarkt-scraper hit
# this on `cf-turnstile`; this repo's own marker set above includes the same
# string, so the same trap applies here). Strip extension-injected script
# tags before matching.
_EXTENSION_TAG_RE = re.compile(
    r"<script[^>]+src=[\"'](?:chrome|moz)-extension://[^\"']+[\"'][^>]*>.*?</script>",
    re.IGNORECASE | re.DOTALL)


def detect_bot_challenge(html: str, url: Optional[str] = None) -> Optional[str]:
    """The challenge vendor found in `html`, or None."""
    if not html:
        return None
    scrubbed = _EXTENSION_TAG_RE.sub("", html)
    for vendor, markers in BOT_CHALLENGE_MARKERS.items():
        if any(marker in scrubbed for marker in markers):
            return vendor
    return None


def _has_any(html: str, selector_class: str) -> bool:
    return selector_class in html


def _header(headers: Optional[Dict[str, str]], name: str) -> Optional[str]:
    """Case-insensitive header lookup. HTTP header names are
    case-insensitive and every layer here spells them differently — the
    Scraper API echoes upstream's casing, Playwright lowercases, and a
    hand-built dict in a test does whatever it likes."""
    if not headers:
        return None
    target = name.lower()
    for key, value in headers.items():
        if isinstance(key, str) and key.lower() == target:
            return value
    return None


def detect_page_state(html: str, status: Optional[int] = None,
                      url: Optional[str] = None,
                      headers: Optional[Dict[str, str]] = None) -> str:
    """content | blocked | captcha | empty — see page_flow.STATE_POLICY.

    The checks below are ordered by HOW MUCH EACH ONE PROVES, not by how
    cheap it is (§17's classification-order trap). Three of the four
    orderings here were wrong before v0.4.1 and each cost a different bug:

    1. `x-amzn-waf-action` is the front naming its own action. Unambiguous,
       so it goes first and nothing can override it.
    2. An empty body proves a failed fetch and nothing else.
    3. The site's OWN content hooks are an unambiguous POSITIVE signal: an
       interstitial does not carry a populated `table.items`. This now runs
       BEFORE the vendor scan, because the scan can match a marker that is
       not the site's at all — 2Captcha's auto-solve extension injects
       hunter scripts into every page the Scraping Browser loads, and a
       served page flipping to exit 3 on one of those is a failure this
       family has already had twice (§18, §19). `_EXTENSION_TAG_RE` strips
       those too; this ordering is the second line of defence.
    4. The vendor scan then only REFINES the reason for a page that has
       already failed to look like content — which is all §18 ever wanted it
       to do.
    5. A non-2xx status comes LAST among the positive tests, because it
       proves something went wrong without saying what. Putting it first is
       what used to make this site's captcha unsolvable: AWS WAF serves the
       CAPTCHA action under HTTP 405, the early `return "blocked"` fired on
       the status alone, and `STATE_POLICY["blocked"]` has `solve: False` —
       so the run rotated exits, burned its retries and reported exit 3
       without ever offering the solver the one challenge this site
       actually serves.

    The status still vetoes step 3: a non-2xx response is not allowed to be
    read as `content` however promising its body looks, which preserves the
    older behaviour for every case except the one being fixed.
    """
    action = (_header(headers, WAF_ACTION_HEADER) or "").strip().lower()
    if action == "captcha":
        return "captcha"
    if action in ("block", "challenge"):
        # `challenge` is AWS WAF's OTHER action — a silent JS interstitial
        # that clears itself, which amazon-scraper waits out rather than
        # solving. Not observed on this site (only `captcha` has been), and
        # reported as blocked rather than paid for until it is.
        return "blocked"

    if not html:
        return "blocked"

    status_ok = status is None or 200 <= status < 300
    if status_ok and (_has_any(html, 'header class="data-header"')
                      or _has_any(html, 'table class="items"')):
        return "content"

    vendor = detect_bot_challenge(html, url=url)
    if vendor:
        return "captcha" if vendor in SOLVABLE_VENDORS else "blocked"

    if not status_ok:
        return "blocked"

    return "empty"


# ---------------------------------------------------------------------------
# Shared row-cell extraction
# ---------------------------------------------------------------------------

def _clean(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def _to_int(text: Optional[str]) -> Optional[int]:
    text = _clean(text)
    if not text or text == "-":
        return None
    try:
        return int(re.sub(r"[^\d-]", "", text))
    except ValueError:
        return None


def _flags(cell) -> List[str]:
    """Nationality names from every `flaggenrahmen` flag image in a cell.

    A row can show two (dual nationality) — both are kept, in document
    order, matching the family's rule of recording what is actually there
    rather than picking one.
    """
    if cell is None:
        return []
    names = []
    for img in cell.select("img.flaggenrahmen"):
        title = img.get("title") or img.get("alt")
        if title and title not in names:
            names.append(title)
    return names


def _player_cell(td) -> Dict[str, object]:
    """Name, id and football position out of a `td.posrela`-shaped cell.

    Shared by market-values, club-squad and transfers: all three list a
    player through the same nested `table.inline-table` structure — a name
    link in the first row, the position as plain text in the second.
    """
    out: Dict[str, object] = {"name": None, "player_id": None,
                              "position": None, "image_url": None}
    if td is None:
        return out
    link = td.select_one('a[href*="/profil/spieler/"]')
    if link:
        out["name"] = _clean(link.get_text())
        out["player_id"] = player_id_from_url(link.get("href"))
    img = td.select_one("img.bilderrahmen-fixed")
    if img:
        out["image_url"] = img.get("data-src") or img.get("src")
    inline = td.select_one("table.inline-table")
    if inline:
        rows = inline.select("tr")
        if len(rows) >= 2:
            out["position"] = _clean(rows[1].get_text())
    return out


def _club_cell(td) -> Dict[str, Optional[str]]:
    """Club name + id out of a simple club-badge cell (market-values,
    club-squad's "Current club" column)."""
    out = {"club": None, "club_id": None}
    if td is None:
        return out
    link = td.select_one('a[href*="/verein/"]')
    if link:
        out["club"] = _detitle(link.get("title"), _clean(link.get_text()))
        out["club_id"] = club_id_from_url(link.get("href"))
    return out


def _club_cell_with_league(td) -> Dict[str, Optional[str]]:
    """Club + id + league out of a transfers-listing "Left"/"Joined" cell.

    Same nested inline-table shape as `_player_cell`, but the second row
    holds a league link (with a flag) instead of a football position.
    """
    out = {"club": None, "club_id": None, "league": None}
    if td is None:
        return out
    # `td.hauptlink` specifically, not just any `/startseite/verein/` link:
    # the same cell also carries a badge-image link in a separate rowspan=2
    # `<td>` that wraps only an `<img>` (no text). Selecting the first match
    # regardless of which `<td>` it is in picked the image link and its
    # empty text, which meant `_detitle` never had real text to fall back to
    # — see that function's docstring for the "Without ClubWithout Club"
    # bug this produced.
    club_link = td.select_one('td.hauptlink a[href*="/startseite/verein/"]')
    if club_link:
        out["club"] = _detitle(club_link.get("title"), _clean(club_link.get_text()))
        out["club_id"] = club_id_from_url(club_link.get("href"))
    league_link = td.select_one('table.inline-table tr:nth-of-type(2) a[href*="/wettbewerb/"]')
    if league_link:
        out["league"] = _clean(league_link.get_text())
    return out


def _fee_cell(
    td, locale: str = DEFAULT_LOCALE
) -> Tuple[Optional[float], Optional[str], Optional[str]]:
    """(fee_eur, currency, fee_type) out of a transfers-listing Fee cell.

    "Loan transfer"/"Free transfer" are matched as literal English text —
    this repo has no non-English transfers-listing capture to measure their
    de/ru/ko/ja equivalents from (see README "Known limitations"), so a
    locale transfers page would fall through to `parse_market_value`
    (locale-aware) for those two rows, get (None, None), and correctly
    report "unknown" rather than mis-report a real loan/free move as
    something else — a degraded-but-honest result, not a crash.
    """
    if td is None:
        return None, None, "unknown"
    text = _clean(td.get_text()) or ""
    lowered = text.lower()
    if lowered == "loan transfer":
        return None, None, "loan"
    if lowered == "free transfer":
        return None, None, "free"
    if text == "?":
        return None, None, "undisclosed"
    if text == "-" or not text:
        return None, None, "unknown"
    amount, currency = parse_market_value(text, locale=locale)
    if amount is None:
        return None, None, "unknown"
    return amount, currency, "disclosed"


# ---------------------------------------------------------------------------
# Listing: market-values
# ---------------------------------------------------------------------------

def parse_market_values(html: str, base_url: str, page_num: int = 1) -> List[Player]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("#yw1 table.items") or soup.select_one("table.items")
    rows: List[Player] = []
    if table is None:
        logger.warning("market-values page %d: no table.items found -- "
                       "0 rows. A markup change, or a page this repo does "
                       "not recognise as the ranking table.", page_num)
        return rows
    locale = locale_of(base_url)
    source = site_host(base_url) or "transfermarkt.com"
    candidates = table.select("tbody > tr")
    skipped = 0
    for i, tr in enumerate(candidates):
        cells = tr.find_all("td", recursive=False)
        if len(cells) < 6:
            skipped += 1
            continue
        player = _player_cell(cells[1])
        if not player.get("player_id"):
            # A footer/total row, or a markup shape this parser does not
            # recognise — never a real player row, either way.
            skipped += 1
            continue
        club = _club_cell(cells[4])
        flags = _flags(cells[3])
        price, currency = parse_market_value(cells[5].get_text(), locale=locale)
        rows.append(Player(
            source=source,
            url=urljoin(base_url, f"/x/profil/spieler/{player['player_id']}"),
            sku=player["player_id"],
            title=player["name"],
            image_url=player["image_url"],
            price=price, currency=currency,
            price_source="listing" if price is not None else None,
            category="market-values",
            rank=_to_int(cells[0].get_text()),
            page=page_num, row_index=i,
            position=translate_position(player["position"], locale=locale),
            age=_to_int(cells[2].get_text()),
            nationality=translate_nationality(flags[0], locale=locale) if flags else None,
            nationalities=[translate_nationality(n, locale=locale) for n in flags] or None,
            club=club["club"], club_id=club["club_id"],
        ))
    if skipped:
        logger.info("market-values page %d: %d/%d candidate row(s) skipped "
                    "(no recognisable player cell -- a footer/total row is "
                    "expected here, several is not).",
                    page_num, skipped, len(candidates))
    return rows


# ---------------------------------------------------------------------------
# Listing: club-squad
# ---------------------------------------------------------------------------

def parse_club_squad(html: str, base_url: str, club_id: Optional[str] = None) -> List[Player]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("#yw1 table.items") or soup.select_one("table.items")
    rows: List[Player] = []
    if table is None:
        logger.warning("club-squad%s: no table.items found -- 0 rows. A "
                       "markup change, or a club id with no current-season "
                       "squad data (which is a real, empty answer -- see "
                       "page_flow.py's 'empty' state).",
                       f" (club {club_id})" if club_id else "")
        return rows
    locale = locale_of(base_url)
    source = site_host(base_url) or "transfermarkt.com"
    candidates = table.select("tbody > tr")
    skipped = 0
    for i, tr in enumerate(candidates):
        cells = tr.find_all("td", recursive=False)
        if len(cells) < 6:
            skipped += 1
            continue
        player = _player_cell(cells[1])
        if not player.get("player_id"):
            skipped += 1
            continue
        club = _club_cell(cells[4])
        flags = _flags(cells[3])
        price, currency = parse_market_value(cells[5].get_text(), locale=locale)
        shirt = cells[0].select_one("div.rn_nummer")
        rows.append(Player(
            source=source,
            url=urljoin(base_url, f"/x/profil/spieler/{player['player_id']}"),
            sku=player["player_id"],
            title=player["name"],
            image_url=player["image_url"],
            price=price, currency=currency,
            price_source="listing" if price is not None else None,
            category=f"club-squad/{club_id}" if club_id else "club-squad",
            page=1, row_index=i,
            position=translate_position(player["position"], locale=locale),
            shirt_number=_to_int(shirt.get_text()) if shirt else None,
            age=_to_int(cells[2].get_text()),
            nationality=translate_nationality(flags[0], locale=locale) if flags else None,
            nationalities=[translate_nationality(n, locale=locale) for n in flags] or None,
            club=club["club"] or None, club_id=club["club_id"] or club_id,
        ))
    if skipped:
        logger.info("club-squad%s: %d/%d candidate row(s) skipped (no "
                    "recognisable player cell).",
                    f" (club {club_id})" if club_id else "",
                    skipped, len(candidates))
    return rows


# ---------------------------------------------------------------------------
# Listing: transfers
# ---------------------------------------------------------------------------

def parse_transfers(html: str, base_url: str, page_num: int = 1) -> List[Transfer]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("#yw1 table.items") or soup.select_one("table.items")
    rows: List[Transfer] = []
    if table is None:
        logger.warning("transfers page %d: no table.items found -- 0 rows. "
                       "A markup change, or a page number beyond the "
                       "listing's real depth.", page_num)
        return rows
    locale = locale_of(base_url)
    source = site_host(base_url) or "transfermarkt.com"
    candidates = table.select("tbody > tr")
    skipped = 0
    for i, tr in enumerate(candidates):
        cells = tr.find_all("td", recursive=False)
        if len(cells) < 6:
            skipped += 1
            continue
        player = _player_cell(cells[0])
        fee_link = cells[5].select_one('a[href*="/jumplist/transfers/spieler/"]')
        transfer_id = transfer_id_from_url(fee_link.get("href")) if fee_link else None
        if not player.get("player_id") or not transfer_id:
            skipped += 1
            continue
        from_club = _club_cell_with_league(cells[3])
        to_club = _club_cell_with_league(cells[4])
        flags = _flags(cells[2])
        fee, currency, fee_type = _fee_cell(cells[5], locale=locale)
        rows.append(Transfer(
            source=source,
            url=urljoin(base_url, fee_link.get("href")) if fee_link else base_url,
            sku=transfer_id,
            title=player["name"],
            image_url=player["image_url"],
            price=fee, currency=currency,
            category="latest-transfers",
            page=page_num, row_index=i,
            player_id=player["player_id"],
            position=translate_position(player["position"], locale=locale),
            age=_to_int(cells[1].get_text()),
            nationality=translate_nationality(flags[0], locale=locale) if flags else None,
            nationalities=[translate_nationality(n, locale=locale) for n in flags] or None,
            from_club=from_club["club"], from_club_id=from_club["club_id"],
            from_league=from_club["league"],
            to_club=to_club["club"], to_club_id=to_club["club_id"],
            to_league=to_club["league"],
            fee_type=fee_type,
        ))
    if skipped:
        logger.info("transfers page %d: %d/%d candidate row(s) skipped (no "
                    "recognisable player id or transfer id).",
                    page_num, skipped, len(candidates))
    return rows


# ---------------------------------------------------------------------------
# Detail: player profile
# ---------------------------------------------------------------------------
_HEIGHT_RE = re.compile(r"([\d]+),(\d+)\s*m")


def _label_map(header) -> Dict[str, str]:
    """{label: content} out of every `data-header__label` / `__content`
    pair in the profile header — a generic map rather than a hardcoded
    position for each field, so a header that reorders or omits a row (an
    unpaid agent, a player with no international caps) does not shift every
    field after it.

    The site uses the SAME class name on two different tags for this:
    `<li class="data-header__label">` in the birth-date/position/agent
    block, and `<span class="data-header__label">` for "Joined"/"Contract
    expires" inside the club-info block. Selecting only `li` silently
    dropped both of the latter — measured on the Haaland capture, where it
    produced `joined_date=None` / `contract_until=None` despite both being
    present on the page.
    """
    out: Dict[str, str] = {}
    for el in header.select("li.data-header__label, span.data-header__label"):
        content = el.select_one("span.data-header__content")
        label = _clean(el.get_text())
        value = _clean(content.get_text()) if content else None
        if not label or value is None:
            continue
        # The label text includes the value's own text (nested spans), so
        # the label proper is what precedes the first colon.
        key = label.split(":", 1)[0].strip().lower()
        out[key] = value
    return out


def _detitle(name: Optional[str], text: Optional[str]) -> Optional[str]:
    """Prefer `text` when `name` (a `title` attribute) is that same text
    doubled — a real markup bug on this site's own "Without Club" free-agent
    placeholder, measured on the 2026-09-10 transfers capture:
    `title="Without ClubWithout Club"` against link text "Without Club".
    Any other title is trusted as given; this only catches the exact
    doubling, so a club whose real name happens to repeat a word is not
    second-guessed.
    """
    if name and text and name == text + text:
        return text
    return name or text


def _text_or_title(el) -> Optional[str]:
    """An element's text, preferring a `title` attribute on a `.cp` child
    when one is visually truncated.

    The site clips long values with CSS and an ellipsis (class `cp`,
    "clipped") while keeping the untruncated string in `title` — measured on
    the player-header's birthplace span for Lamine Yamal, 2026-09-10:
    `<span class="cp" title="Esplugues de Llobregat">Esplugues de ...</span>`.
    `.get_text()` alone silently ships the clipped "Esplugues de ..." — the
    same class of bug `_detitle` already guards against for a doubled link
    `title`, just triggered by truncation instead of duplication. Checked
    against a real capture; this is a DEFENSIVE fallback path only — see
    `_info_table_map`, which is the PRIMARY source for the one field this
    was measured to affect (`birth_place`) precisely because that box's
    copy of the same value is never truncated at all.
    """
    if el is None:
        return None
    clipped = el.select_one(".cp[title]") if hasattr(el, "select_one") else None
    if clipped is None and "cp" in (el.get("class") or []):
        clipped = el
    if clipped is not None:
        title = clipped.get("title")
        if title:
            return _clean(title)
    return _clean(el.get_text())


def _info_table_map(soup) -> Dict[str, str]:
    """{label: value} from the player profile's "Facts and data" panel
    (`div.info-table`, `info-table__content--regular` label /
    `--bold` value spans, alternating siblings) — a SEPARATE DOM location
    from `_label_map`'s header block, and the only place two things live:

    1. The player's real full name. The site does not use one consistent
       label for it: a "Full name" row (Lamine Yamal:
       "Lamine Yamal Nasraoui Ebana") on some profiles, a "Name in home
       country" row (Erling Haaland: "Erling Braut Håland" — his native
       spelling, with the middle name his display name omits) on others —
       both measured 2026-09-10. `parse_player_detail` checks both keys.
       Neither is the display name (the h1 headline), and prior to this fix
       this file did not read this box at all: `full_name` silently
       duplicated the display name on every row, a regression from the
       pre-rewrite scraper, which read this exact box generically via
       `.info-table__content--regular` (see CHANGELOG).
    2. An UNTRUNCATED copy of `Place of birth` — the header's own copy of
       the same value sits in a clipped `.cp` span (see `_text_or_title`);
       this box's copy is not clipped, so it is the primary source and the
       header is only a fallback for a profile missing this panel.

    The box also carries foot, agent, current club, outfitter and
    social-media rows this repo does not map to a column (v0.1 scope — see
    README "Known limitations"); reading it here for two fields rather than
    building a second full extractor keeps this addition narrow.
    """
    out: Dict[str, str] = {}
    box = soup.select_one("div.info-table")
    if box is None:
        return out
    for label_el in box.select("span.info-table__content--regular"):
        value_el = label_el.find_next_sibling("span", class_="info-table__content--bold")
        if value_el is None:
            continue
        key = _clean(label_el.get_text())
        if not key:
            continue
        key = key.rstrip(":").strip().lower()
        value = _clean(value_el.get_text(" "))
        if value:
            out[key] = value
    return out


def parse_player_detail(html: str, base_url: str) -> Optional[Player]:
    soup = BeautifulSoup(html, "html.parser")
    header = soup.select_one('header.data-header[itemtype="https://schema.org/Person"]')
    if header is None:
        logger.warning("player detail %s: no schema.org/Person header found "
                       "-- returning no row. A markup change, or a URL that "
                       "is not actually a player profile page.", base_url)
        return None

    locale = locale_of(base_url)
    source = site_host(base_url) or "transfermarkt.com"
    player_id = player_id_from_url(base_url)
    h1 = header.select_one("h1.data-header__headline-wrapper")
    name = None
    shirt_number = None
    if h1:
        shirt_el = h1.select_one("span.data-header__shirt-number")
        if shirt_el:
            shirt_number = _to_int(shirt_el.get_text().lstrip("#"))
        # The name is the h1's text with the shirt-number span removed.
        h1_copy = BeautifulSoup(str(h1), "html.parser")
        num = h1_copy.select_one("span.data-header__shirt-number")
        if num:
            num.decompose()
        name = _clean(h1_copy.get_text())

    club_link = header.select_one("span.data-header__club a")
    league_link = header.select_one("a.data-header__league-link")
    mv_wrapper = header.select_one("a.data-header__market-value-wrapper")
    price, currency = (None, None)
    mv_last_update = None
    if mv_wrapper:
        price, currency = parse_market_value(mv_wrapper.get_text(), locale=locale)
        update_el = mv_wrapper.select_one("p.data-header__last-update")
        if update_el:
            prefix = _LAST_UPDATE_PREFIX.get(locale, _LAST_UPDATE_PREFIX[DEFAULT_LOCALE])
            mv_last_update = _clean(update_el.get_text()).replace(prefix, "").strip()
            mv_last_update = normalize_date(mv_last_update, locale=locale)

    birth = header.select_one('[itemprop="birthDate"]')
    birth_place = header.select_one('[itemprop="birthPlace"]')
    nationality_el = header.select_one('[itemprop="nationality"]')
    height_el = header.select_one('[itemprop="height"]')

    labels = _label_map(header)
    info = _info_table_map(soup)
    height_m = None
    if height_el:
        m = _HEIGHT_RE.search(height_el.get_text())
        if m:
            height_m = float(f"{m.group(1)}.{m.group(2)}")

    agent_link = header.select_one("li.data-header__label a[href*='/berater/']")
    caps_links = header.select('a.data-header__content--highlight[href*="/nationalmannschaft/"]')

    flags = _flags(nationality_el) if nationality_el else []
    birth_date_text = _clean(birth.get_text()) if birth is not None else None
    # "21/07/2000 (26)" — the age in parentheses is derivable and not kept
    # as a separate field on a detail row; `birth_date` keeps the (now
    # ISO-normalised) date. The " (" split is locale-independent: measured
    # on all five locales, the age always trails the date in a space + "("
    # (KO's own digit-suffix "26세" starts right after "(" the same way).
    birth_date_raw = birth_date_text.split(" (")[0].strip() if birth_date_text else None
    birth_date = normalize_date(birth_date_raw, locale=locale) if birth_date_raw else None
    age = None
    if birth_date_text and "(" in birth_date_text:
        age = _to_int(birth_date_text.split("(")[-1].rstrip(")"))

    return Player(
        source=source,
        url=base_url,
        sku=player_id,
        title=name,
        image_url=(header.select_one("img.data-header__profile-image") or {}).get("src")
        if header.select_one("img.data-header__profile-image") else None,
        price=price, currency=currency,
        price_source="detail" if price is not None else None,
        category="player",
        position=translate_position(_label(labels, "position", locale), locale=locale),
        shirt_number=shirt_number,
        age=age,
        nationality=translate_nationality(flags[0], locale=locale) if flags else None,
        nationalities=[translate_nationality(n, locale=locale) for n in flags] or None,
        club=club_link.get("title") if club_link else None,
        club_id=club_id_from_url(club_link.get("href")) if club_link else None,
        full_name=_info(info, "full name", locale) or _info(info, "name in home country", locale),
        birth_date=birth_date,
        birth_place=_info(info, "place of birth", locale) or _text_or_title(birth_place),
        height_m=height_m,
        agent=_clean(agent_link.get_text()) if agent_link else None,
        league=_clean(league_link.get_text()) if league_link else None,
        league_id=league_id_from_url(league_link.get("href")) if league_link else None,
        joined_date=normalize_date(_label(labels, "joined", locale), locale=locale),
        contract_until=normalize_date(_label(labels, "contract expires", locale), locale=locale),
        market_value_last_update=mv_last_update,
        caps=_to_int(caps_links[0].get_text()) if len(caps_links) >= 1 else None,
        international_goals=_to_int(caps_links[1].get_text()) if len(caps_links) >= 2 else None,
    )
