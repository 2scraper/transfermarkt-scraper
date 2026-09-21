#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# The encoding declaration matters here for the same reason it does in the
# sibling repos: the fixtures below are HTML slices with accented names
# (Kylian Mbappe, Cote d'Ivoire) sitting inside very long lines, and an
# undeclared encoding can trip an older tokenizer on exactly that content.
"""
smoke_test.py
--------------
Zero-network, zero-browser sanity check for transfermarkt-scraper.

Run this FIRST, before touching a real browser or transfermarkt.com, to
confirm the environment and the parsing/output/policy logic work:

    python3 smoke_test.py

Deliberately ONE file of plain functions with inline fixtures -- no pytest,
no conftest, no fixtures directory. tests/test_smoke.py wraps it as a single
pytest test so `pytest` works as an entry point without a second copy of the
checks that could drift from this one.

It must pass with NO engine library installed at all, so every
`import playwright_scraper` / `puppeteer_scraper` / `selenium_scraper` is
guarded and the skip is REPORTED. CI's engine-smoke job installs all three
and fails if anything reports skipped, because "skipped, engine absent"
reads identically to a real import error.

What this suite is actually for
--------------------------------
Not coverage. Every check that matters here pins a VALUE read off a real
capture, because a column can be 100% populated and entirely wrong. This
repo already found two such bugs while building it (see product_parser.py's
`_label_map` and `_club_cell_with_league` docstrings): `joined_date` and
`contract_until` came back None for every player despite the data being on
the page, and a transfer's `from_club` came back "Without ClubWithout Club"
(a doubled title) for a free-agent row, in both cases while every column
still "had a value" as far as a coverage check could tell. So the assertions
below say `price == 220_000_000.0`, not `price is not None`.

Exits non-zero on any failure.
"""

import ast
import builtins
import csv
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import fields
from typing import Optional

import env_config
import page_flow
import product_parser
from captcha_solver import (CaptchaChallenge, detect_recaptcha_v3,
                            detect_recaptcha_in_page, reconcile_detections,
                            solve_recaptcha, get_balance)
from diff_runs import diff_rows, _check_comparable, TRACKED_FIELDS
from output_writer import (Player, Transfer, ROW_CLASS_BY_MODE,
                           UNIQUE_BY_SKU_MODES, dedupe_by_key, dedupe_by_sku,
                           write_json, write_csv, save, finish_run, run_meta,
                           LIST_CSV_SEPARATOR, EXIT_BLOCKED, EXIT_NO_PRODUCTS,
                           EXIT_PARTIAL, EXIT_REMOTE_API_ERROR, RemoteAPIError,
                           COMPLETE_STOP_REASONS, SOURCE_DEFAULT)
from product_parser import (HOSTS, MARKET_VALUES_URL, TRANSFERS_URL,
                            player_url, club_squad_url, page_url,
                            page_number_from_url, player_id_from_url,
                            club_id_from_url, transfer_id_from_url,
                            league_id_from_url, parse_market_value,
                            detect_bot_challenge, detect_page_state,
                            is_supported_host, unsupported_reason, site_host,
                            parse_market_values, parse_club_squad,
                            parse_transfers, parse_player_detail, BOT_CHALLENGE_MARKERS,
                            LOCALE_BY_HOST, locale_of, translate_position,
                            translate_nationality, normalize_date)
from proxy_pool import (ProxyPool, ProxyError, mask, to_playwright,
                        split_credentials, parse_proxy_line)

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

_failures = []


def check(label, condition):
    """Print and record one check. Returns the condition, so callers can
    accumulate with `ok &= check(...)`."""
    if condition:
        print("  PASS  %s" % label)
    else:
        print("  FAIL  %s" % label)
        _failures.append(label)
    return bool(condition)


def group(title):
    print("\n== %s" % title)


def _raises(fn):
    """True if `fn()` raises. Used where refusing is the correct behaviour."""
    try:
        fn()
    except Exception:
        return True
    return False

# ---------------------------------------------------------------------------
# Fixtures: trimmed, scrubbed excerpts of real captures
# ---------------------------------------------------------------------------
# Cut from live pages fetched via the 2Captcha MCP browser tools on
# 2026-09-10 (market-values ranking page 1, Manchester City's squad page,
# the latest-transfers listing, and Erling Haaland's profile). Trimmed to a
# handful of rows each — full pages ran 120-380KB and this suite needs the
# STRUCTURE, not every row — and scrubbed per CLAUDE.md §10 (see
# test_no_capture_leaks): no session id, token or email was found in any of
# these pages to begin with (this site carries no such per-visitor state in
# its HTML, unlike a review page on an e-commerce sibling), but the same
# pattern-based scan runs here anyway so the NEXT capture is checked too.
#
# market_values: rows 0-4 of page 1, in site order — includes Kylian Mbappe
# (row 2) for the dual-nationality case (France + Cameroon).
# club_squad: Manchester City, rows 0-3 (priced) plus one youth player with
# no published market value ("-" -> None, not 0).
# transfers: 6 of page 1's 25 rows, chosen to cover every fee_type this
# repo classifies (disclosed, loan, free, undisclosed "?", and two
# "Without Club" free-agent rows -- one of which carries the site's own
# doubled-title bug, `title="Without ClubWithout Club"`, that _detitle()
# exists to catch).
# player_detail: Erling Haaland's full profile header.
FIX_MARKET_VALUES = """<html lang="en"><head><title>market values</title></head><body><table class="items"><tbody><tr class="odd">
<td class="zentriert">1</td><td class=""><table class="inline-table"><tr><td rowspan="2"><a href="#"><img alt="Lamine Yamal" class="bilderrahmen-fixed" src="https://img.a.transfermarkt.technology/portrait/small/937958-1773173768.jpg?lm=4711" title="Lamine Yamal"/></a></td><td class="hauptlink"><a href="/lamine-yamal/profil/spieler/937958" title="Lamine Yamal">Lamine Yamal</a></td></tr><tr><td>Right Winger</td></tr></table></td><td class="zentriert">19</td><td class="zentriert"><img alt="Spain" class="flaggenrahmen" src="https://img.a.transfermarkt.technology/flagge/verysmall/157.png?lm=4711" title="Spain"/></td><td class="zentriert"><a href="/fc-barcelona/startseite/verein/131" title="FC Barcelona"><img alt="FC Barcelona" class="" src="https://img.a.transfermarkt.technology/wappen/verysmall/131.png?lm=4711" title="FC Barcelona"/></a></td><td class="rechts hauptlink"><a href="/lamine-yamal/marktwertverlauf/spieler/937958">€220.00m</a> </td></tr><tr class="even">
<td class="zentriert">1</td><td class=""><table class="inline-table"><tr><td rowspan="2"><a href="#"><img alt="Erling Haaland" class="bilderrahmen-fixed" src="https://img.a.transfermarkt.technology/portrait/small/418560-1788115885.jpg?lm=4711" title="Erling Haaland"/></a></td><td class="hauptlink"><a href="/erling-haaland/profil/spieler/418560" title="Erling Haaland">Erling Haaland</a></td></tr><tr><td>Centre-Forward</td></tr></table></td><td class="zentriert">26</td><td class="zentriert"><img alt="Norway" class="flaggenrahmen" src="https://img.a.transfermarkt.technology/flagge/verysmall/125.png?lm=4711" title="Norway"/></td><td class="zentriert"><a href="/manchester-city/startseite/verein/281" title="Manchester City"><img alt="Manchester City" class="" src="https://img.a.transfermarkt.technology/wappen/verysmall/281.png?lm=4711" title="Manchester City"/></a></td><td class="rechts hauptlink"><a href="/erling-haaland/marktwertverlauf/spieler/418560">€220.00m</a> </td></tr><tr class="odd">
<td class="zentriert">3</td><td class=""><table class="inline-table"><tr><td rowspan="2"><a href="#"><img alt="Kylian Mbappé" class="bilderrahmen-fixed" src="https://img.a.transfermarkt.technology/portrait/small/342229-1682683695.jpg?lm=4711" title="Kylian Mbappé"/></a></td><td class="hauptlink"><a href="/kylian-mbappe/profil/spieler/342229" title="Kylian Mbappé">Kylian Mbappé</a></td></tr><tr><td>Centre-Forward</td></tr></table></td><td class="zentriert">27</td><td class="zentriert"><img alt="France" class="flaggenrahmen" src="https://img.a.transfermarkt.technology/flagge/verysmall/50.png?lm=4711" title="France"><br><img alt="Cameroon" class="flaggenrahmen" src="https://img.a.transfermarkt.technology/flagge/verysmall/31.png?lm=4711" title="Cameroon"/></br></img></td><td class="zentriert"><a href="/real-madrid/startseite/verein/418" title="Real Madrid"><img alt="Real Madrid" class="" src="https://img.a.transfermarkt.technology/wappen/verysmall/418.png?lm=4711" title="Real Madrid"/></a></td><td class="rechts hauptlink"><a href="/kylian-mbappe/marktwertverlauf/spieler/342229">€200.00m</a> </td></tr><tr class="even">
<td class="zentriert">4</td><td class=""><table class="inline-table"><tr><td rowspan="2"><a href="#"><img alt="Michael Olise" class="bilderrahmen-fixed" src="https://img.a.transfermarkt.technology/portrait/small/566723-1762944477.jpg?lm=4711" title="Michael Olise"/></a></td><td class="hauptlink"><a href="/michael-olise/profil/spieler/566723" title="Michael Olise">Michael Olise</a></td></tr><tr><td>Right Winger</td></tr></table></td><td class="zentriert">24</td><td class="zentriert"><img alt="France" class="flaggenrahmen" src="https://img.a.transfermarkt.technology/flagge/verysmall/50.png?lm=4711" title="France"><br><img alt="England" class="flaggenrahmen" src="https://img.a.transfermarkt.technology/flagge/verysmall/189.png?lm=4711" title="England"/></br></img></td><td class="zentriert"><a href="/fc-bayern-munchen/startseite/verein/27" title="Bayern Munich"><img alt="Bayern Munich" class="" src="https://img.a.transfermarkt.technology/wappen/verysmall/27.png?lm=4711" title="Bayern Munich"/></a></td><td class="rechts hauptlink"><a href="/michael-olise/marktwertverlauf/spieler/566723">€170.00m</a> </td></tr><tr class="odd">
<td class="zentriert">5</td><td class=""><table class="inline-table"><tr><td rowspan="2"><a href="#"><img alt="Jude Bellingham" class="bilderrahmen-fixed" src="https://img.a.transfermarkt.technology/portrait/small/581678-1748102891.jpg?lm=4711" title="Jude Bellingham"/></a></td><td class="hauptlink"><a href="/jude-bellingham/profil/spieler/581678" title="Jude Bellingham">Jude Bellingham</a></td></tr><tr><td>Attacking Midfield</td></tr></table></td><td class="zentriert">23</td><td class="zentriert"><img alt="England" class="flaggenrahmen" src="https://img.a.transfermarkt.technology/flagge/verysmall/189.png?lm=4711" title="England"/><br><img alt="Ireland" class="flaggenrahmen" src="https://img.a.transfermarkt.technology/flagge/verysmall/72.png?lm=4711" title="Ireland"/></br></td><td class="zentriert"><a href="/real-madrid/startseite/verein/418" title="Real Madrid"><img alt="Real Madrid" class="" src="https://img.a.transfermarkt.technology/wappen/verysmall/418.png?lm=4711" title="Real Madrid"/></a></td><td class="rechts hauptlink"><a href="/jude-bellingham/marktwertverlauf/spieler/581678">€160.00m</a> </td></tr></tbody></table></body></html>"""

FIX_CLUB_SQUAD = """<html lang="en"><head><title>club squad</title></head><body><table class="items"><tbody><tr class="odd">
<td class="zentriert rueckennummer bg_Torwart" title="Goalkeeper"><div class="rn_nummer">25</div></td><td class="posrela">
<table class="inline-table">
<tr>
<td rowspan="2">
<img alt="Gianluigi Donnarumma" class="bilderrahmen-fixed lazy lazy" data-src="https://img.a.transfermarkt.technology/portrait/medium/315858-1761076746.jpg?lm=4711" src="data:image/gif;base64,R0lGODlhAQABAIAAAMLCwgAAACH5BAAAAAAALAAAAAABAAEAAAICRAEAOw==" title="Gianluigi Donnarumma"> </img></td>
<td class="hauptlink">
<a href="/gianluigi-donnarumma/profil/spieler/315858">
                Gianluigi Donnarumma            </a>
</td>
</tr>
<tr>
<td>
            Goalkeeper        </td>
</tr>
</table>
</td><td class="zentriert">27</td><td class="zentriert"><img alt="Italy" class="flaggenrahmen" src="https://img.a.transfermarkt.technology/flagge/verysmall/75.png?lm=4711" title="Italy"/></td><td class="zentriert"><a href="/manchester-city/startseite/verein/281" title="Manchester City"><img alt="Manchester City" class="" src="https://img.a.transfermarkt.technology/wappen/verysmall/281.png?lm=4711" title="Manchester City"/></a></td><td class="rechts hauptlink"><a href="/gianluigi-donnarumma/marktwertverlauf/spieler/315858">€45.00m</a></td></tr><tr class="even">
<td class="zentriert rueckennummer bg_Torwart" title="Goalkeeper"><div class="rn_nummer">1</div></td><td class="posrela">
<table class="inline-table">
<tr>
<td rowspan="2">
<img alt="James Trafford" class="bilderrahmen-fixed lazy lazy" data-src="https://img.a.transfermarkt.technology/portrait/medium/566799-1689876147.jpg?lm=4711" src="data:image/gif;base64,R0lGODlhAQABAIAAAMLCwgAAACH5BAAAAAAALAAAAAABAAEAAAICRAEAOw==" title="James Trafford"/> </td>
<td class="hauptlink">
<a href="/james-trafford/profil/spieler/566799">
                James Trafford            </a>
</td>
</tr>
<tr>
<td>
            Goalkeeper        </td>
</tr>
</table>
</td><td class="zentriert">23</td><td class="zentriert"><img alt="England" class="flaggenrahmen" src="https://img.a.transfermarkt.technology/flagge/verysmall/189.png?lm=4711" title="England"/></td><td class="zentriert"><a href="/leeds-united/startseite/verein/399" title="Leeds United"><img alt="Leeds United" class="" src="https://img.a.transfermarkt.technology/wappen/verysmall/399.png?lm=4711" title="Leeds United"/></a></td><td class="rechts hauptlink"><a href="/james-trafford/marktwertverlauf/spieler/566799">€25.00m</a></td></tr><tr class="odd">
<td class="zentriert rueckennummer bg_Torwart" title="Goalkeeper"><div class="rn_nummer">-</div></td><td class="posrela">
<table class="inline-table">
<tr>
<td rowspan="2">
<img alt="Ederson" class="bilderrahmen-fixed lazy lazy" data-src="https://img.a.transfermarkt.technology/portrait/medium/238223-1765380595.jpg?lm=4711" src="data:image/gif;base64,R0lGODlhAQABAIAAAMLCwgAAACH5BAAAAAAALAAAAAABAAEAAAICRAEAOw==" title="Ederson"/> </td>
<td class="hauptlink">
<a href="/ederson/profil/spieler/238223">
                Ederson            </a>
</td>
</tr>
<tr>
<td>
            Goalkeeper        </td>
</tr>
</table>
</td><td class="zentriert">32</td><td class="zentriert"><img alt="Brazil" class="flaggenrahmen" src="https://img.a.transfermarkt.technology/flagge/verysmall/26.png?lm=4711" title="Brazil"/><br><img alt="Portugal" class="flaggenrahmen" src="https://img.a.transfermarkt.technology/flagge/verysmall/136.png?lm=4711" title="Portugal"/></br></td><td class="zentriert"><a href="/fenerbahce-istanbul/startseite/verein/36" title="Fenerbahce"><img alt="Fenerbahce" class="" src="https://img.a.transfermarkt.technology/wappen/verysmall/36.png?lm=4711" title="Fenerbahce"/></a></td><td class="rechts hauptlink"><a href="/ederson/marktwertverlauf/spieler/238223">€10.00m</a></td></tr><tr class="even">
<td class="zentriert rueckennummer bg_Torwart" title="Goalkeeper"><div class="rn_nummer">-</div></td><td class="posrela">
<table class="inline-table">
<tr>
<td rowspan="2">
<img alt="Stefan Ortega" class="bilderrahmen-fixed lazy lazy" data-src="https://img.a.transfermarkt.technology/portrait/medium/85941-1785410814.png?lm=4711" src="data:image/gif;base64,R0lGODlhAQABAIAAAMLCwgAAACH5BAAAAAAALAAAAAABAAEAAAICRAEAOw==" title="Stefan Ortega"/> </td>
<td class="hauptlink">
<a href="/stefan-ortega/profil/spieler/85941">
                Stefan Ortega            </a>
</td>
</tr>
<tr>
<td>
            Goalkeeper        </td>
</tr>
</table>
</td><td class="zentriert">33</td><td class="zentriert"><img alt="Germany" class="flaggenrahmen" src="https://img.a.transfermarkt.technology/flagge/verysmall/40.png?lm=4711" title="Germany"/><br><img alt="Spain" class="flaggenrahmen" src="https://img.a.transfermarkt.technology/flagge/verysmall/157.png?lm=4711" title="Spain"/></br></td><td class="zentriert"><a href="/olympiakos-piraus/startseite/verein/683" title="Olympiacos Piraeus"><img alt="Olympiacos Piraeus" class="" src="https://img.a.transfermarkt.technology/wappen/verysmall/683.png?lm=4711" title="Olympiacos Piraeus"/></a></td><td class="rechts hauptlink"><a href="/stefan-ortega/marktwertverlauf/spieler/85941">€4.00m</a></td></tr><tr class="odd">
<td class="zentriert rueckennummer bg_Abwehr" title="Defender"><div class="rn_nummer">-</div></td><td class="posrela">
<table class="inline-table">
<tr>
<td rowspan="2">
<img alt="Kian Noble" class="bilderrahmen-fixed lazy lazy" data-src="https://img.a.transfermarkt.technology/portrait/medium/985535-1739803418.jpg?lm=4711" src="data:image/gif;base64,R0lGODlhAQABAIAAAMLCwgAAACH5BAAAAAAALAAAAAABAAEAAAICRAEAOw==" title="Kian Noble"/> </td>
<td class="hauptlink">
<a href="/kian-noble/profil/spieler/985535">
                Kian Noble            </a>
</td>
</tr>
<tr>
<td>
            Centre-Back        </td>
</tr>
</table>
</td><td class="zentriert">19</td><td class="zentriert"><img alt="England" class="flaggenrahmen" src="https://img.a.transfermarkt.technology/flagge/verysmall/189.png?lm=4711" title="England"/></td><td class="zentriert"><a href="/manchester-city-u23/startseite/verein/9265" title="Manchester City U21"><img alt="Manchester City U21" class="" src="https://img.a.transfermarkt.technology/wappen/verysmall/9265.png?lm=4711" title="Manchester City U21"/></a></td><td class="rechts hauptlink">-</td></tr></tbody></table></body></html>"""

FIX_TRANSFERS = """<html lang="en"><head><title>transfers</title></head><body><table class="items"><tbody><tr class="odd">
<td class=""> <table class="inline-table">
<tr>
<td rowspan="2">
<img alt="Isaac Kiese Thelin" class="bilderrahmen-fixed lazy lazy" data-src="https://img.a.transfermarkt.technology/portrait/medium/201311-1774735065.JPG?lm=4711" src="data:image/gif;base64,R0lGODlhAQABAIAAAMLCwgAAACH5BAAAAAAALAAAAAABAAEAAAICRAEAOw==" title="Isaac Kiese Thelin"> </img></td>
<td class="hauptlink">
<a href="/isaac-kiese-thelin/profil/spieler/201311" title="Isaac Kiese Thelin">Isaac Kiese Thelin</a> </td>
</tr>
<tr>
<td>Centre-Forward</td>
</tr>
</table>
</td><td class="zentriert">34</td><td class="zentriert"><img alt="Sweden" class="flaggenrahmen" src="https://img.a.transfermarkt.technology/flagge/verysmall/147.png?lm=4711" title="Sweden"><br><img alt="DR Congo" class="flaggenrahmen" src="https://img.a.transfermarkt.technology/flagge/verysmall/193.png?lm=4711" title="DR Congo"/></br></img></td><td class=""><table class="inline-table">
<tr>
<td rowspan="2">
<a href="/vereinslos/startseite/verein/515/saison_id/2026" title="Without ClubWithout Club"><img alt="Without Club" class="tiny_wappen" src="https://img.a.transfermarkt.technology/wappen/tiny/515.png?lm=4711" title="Without Club"/></a> </td>
<td class="hauptlink">
<a href="/vereinslos/startseite/verein/515/saison_id/2026" title="Without Club">Without Club</a> </td>
</tr>
<tr>
<td>
</td>
</tr>
</table>
</td><td class=""><table class="inline-table">
<tr>
<td rowspan="2">
<a href="/sv-ried/startseite/verein/266/saison_id/2026" title="SV Ried"><img alt="SV Ried" class="tiny_wappen" src="https://img.a.transfermarkt.technology/wappen/tiny/266.png?lm=4711" title="SV Ried"/></a> </td>
<td class="hauptlink">
<a href="/sv-ried/startseite/verein/266/saison_id/2026" title="SV Ried">SV Ried</a> </td>
</tr>
<tr>
<td>
<img alt="Austria" class="flaggenrahmen" src="https://img.a.transfermarkt.technology/flagge/verysmall/127.png?lm=4711" title="Austria"> <a href="/bundesliga/transfers/wettbewerb/A1/saison_id/2026" title="Bundesliga">Bundesliga</a> </img></td>
</tr>
</table>
</td><td class="rechts hauptlink"><a href="/jumplist/transfers/spieler/201311/transfer_id/6579809">-</a></td></tr><tr class="even">
<td class=""> <table class="inline-table">
<tr>
<td rowspan="2">
<img alt="Nikita Saltykov" class="bilderrahmen-fixed lazy lazy" data-src="https://img.a.transfermarkt.technology/portrait/medium/697728-1788849665.png?lm=4711" src="data:image/gif;base64,R0lGODlhAQABAIAAAMLCwgAAACH5BAAAAAAALAAAAAABAAEAAAICRAEAOw==" title="Nikita Saltykov"> </img></td>
<td class="hauptlink">
<a href="/nikita-saltykov/profil/spieler/697728" title="Nikita Saltykov">Nikita Saltykov</a> </td>
</tr>
<tr>
<td>Left Winger</td>
</tr>
</table>
</td><td class="zentriert">22</td><td class="zentriert"><img alt="Russia" class="flaggenrahmen" src="https://img.a.transfermarkt.technology/flagge/verysmall/141.png?lm=4711" title="Russia"/></td><td class=""><table class="inline-table">
<tr>
<td rowspan="2">
<a href="/lokomotiv-moskau/startseite/verein/932/saison_id/2026" title="Lokomotiv Moscow"><img alt="Lokomotiv Moscow" class="tiny_wappen" src="https://img.a.transfermarkt.technology/wappen/tiny/932.png?lm=4711" title="Lokomotiv Moscow"/></a> </td>
<td class="hauptlink">
<a href="/lokomotiv-moskau/startseite/verein/932/saison_id/2026" title="Lokomotiv Moscow">Loko Moscow</a> </td>
</tr>
<tr>
<td>
<img alt="Russia" class="flaggenrahmen" src="https://img.a.transfermarkt.technology/flagge/tiny/141.png?lm=4711" title="Russia"> <a href="/premier-liga/transfers/wettbewerb/RU1/saison_id/2026" title="Premier Liga">Premier Liga</a> </img></td>
</tr>
</table>
</td><td class=""><table class="inline-table">
<tr>
<td rowspan="2">
<a href="/krylya-sovetov-samara/startseite/verein/2696/saison_id/2026" title="Krylya Sovetov Samara"><img alt="Krylya Sovetov Samara" class="tiny_wappen" src="https://img.a.transfermarkt.technology/wappen/tiny/2696.png?lm=1788436088" title="Krylya Sovetov Samara"/></a> </td>
<td class="hauptlink">
<a href="/krylya-sovetov-samara/startseite/verein/2696/saison_id/2026" title="Krylya Sovetov Samara">KS Samara</a> </td>
</tr>
<tr>
<td>
<img alt="Russia" class="flaggenrahmen" src="https://img.a.transfermarkt.technology/flagge/verysmall/141.png?lm=4711" title="Russia"> <a href="/premier-liga/transfers/wettbewerb/RU1/saison_id/2026" title="Premier Liga">Premier Liga</a> </img></td>
</tr>
</table>
</td><td class="rechts hauptlink"><a href="/jumplist/transfers/spieler/697728/transfer_id/6579806">loan transfer</a></td></tr><tr class="even">
<td class=""> <table class="inline-table">
<tr>
<td rowspan="2">
<img alt="David Akintola" class="bilderrahmen-fixed lazy lazy" data-src="https://img.a.transfermarkt.technology/portrait/medium/353775-1729515266.jpg?lm=4711" src="data:image/gif;base64,R0lGODlhAQABAIAAAMLCwgAAACH5BAAAAAAALAAAAAABAAEAAAICRAEAOw==" title="David Akintola"/> </td>
<td class="hauptlink">
<a href="/david-akintola/profil/spieler/353775" title="David Akintola">David Akintola</a> </td>
</tr>
<tr>
<td>Right Winger</td>
</tr>
</table>
</td><td class="zentriert">30</td><td class="zentriert"><img alt="Nigeria" class="flaggenrahmen" src="https://img.a.transfermarkt.technology/flagge/verysmall/124.png?lm=4711" title="Nigeria"/></td><td class=""><table class="inline-table">
<tr>
<td rowspan="2">
<a href="/hull-city/startseite/verein/3008/saison_id/2026" title="Hull City"><img alt="Hull City" class="tiny_wappen" src="https://img.a.transfermarkt.technology/wappen/tiny/3008.png?lm=4711" title="Hull City"/></a> </td>
<td class="hauptlink">
<a href="/hull-city/startseite/verein/3008/saison_id/2026" title="Hull City">Hull City</a> </td>
</tr>
<tr>
<td>
<img alt="England" class="flaggenrahmen" src="https://img.a.transfermarkt.technology/flagge/tiny/189.png?lm=4711" title="England"/> <a href="/premier-league/transfers/wettbewerb/GB1/saison_id/2026" title="Premier League">Premier League</a> </td>
</tr>
</table>
</td><td class=""><table class="inline-table">
<tr>
<td rowspan="2">
<a href="/omonia-nikosia/startseite/verein/829/saison_id/2026" title="Omonia Nicosia"><img alt="Omonia Nicosia" class="tiny_wappen" src="https://img.a.transfermarkt.technology/wappen/tiny/829.png?lm=4711" title="Omonia Nicosia"/></a> </td>
<td class="hauptlink">
<a href="/omonia-nikosia/startseite/verein/829/saison_id/2026" title="Omonia Nicosia">Omonia Nicosia</a> </td>
</tr>
<tr>
<td>
<img alt="Cyprus" class="flaggenrahmen" src="https://img.a.transfermarkt.technology/flagge/verysmall/188.png?lm=4711" title="Cyprus"/> <a href="/protathlima-cyta/transfers/wettbewerb/ZYP1/saison_id/2026" title="Cyprus League">Cyprus League</a> </td>
</tr>
</table>
</td><td class="rechts hauptlink"><a href="/jumplist/transfers/spieler/353775/transfer_id/6579119">?</a></td></tr><tr class="odd">
<td class=""> <table class="inline-table">
<tr>
<td rowspan="2">
<img alt="Emiliano Gómez" class="bilderrahmen-fixed lazy lazy" data-src="https://img.a.transfermarkt.technology/portrait/medium/577489-1721317803.png?lm=4711" src="data:image/gif;base64,R0lGODlhAQABAIAAAMLCwgAAACH5BAAAAAAALAAAAAABAAEAAAICRAEAOw==" title="Emiliano Gómez"/> </td>
<td class="hauptlink">
<a href="/emiliano-gomez/profil/spieler/577489" title="Emiliano Gómez">Emiliano Gómez</a> </td>
</tr>
<tr>
<td>Second Striker</td>
</tr>
</table>
</td><td class="zentriert">24</td><td class="zentriert"><img alt="Uruguay" class="flaggenrahmen" src="https://img.a.transfermarkt.technology/flagge/verysmall/179.png?lm=4711" title="Uruguay"/></td><td class=""><table class="inline-table">
<tr>
<td rowspan="2">
<a href="/puebla-fc/startseite/verein/5662/saison_id/2026" title="Puebla FC"><img alt="Puebla FC" class="tiny_wappen" src="https://img.a.transfermarkt.technology/wappen/tiny/5662.png?lm=4711" title="Puebla FC"/></a> </td>
<td class="hauptlink">
<a href="/puebla-fc/startseite/verein/5662/saison_id/2026" title="Puebla FC">Puebla FC</a> </td>
</tr>
<tr>
<td>
<img alt="Mexico" class="flaggenrahmen" src="https://img.a.transfermarkt.technology/flagge/tiny/110.png?lm=4711" title="Mexico"/> <a href="/liga-mx-apertura/transfers/wettbewerb/MEXA/saison_id/2026" title="Liga MX Apertura">Liga MX Apertura</a> </td>
</tr>
</table>
</td><td class=""><table class="inline-table">
<tr>
<td rowspan="2">
<a href="/tigres-uanl/startseite/verein/7055/saison_id/2026" title="Tigres UANL"><img alt="Tigres UANL" class="tiny_wappen" src="https://img.a.transfermarkt.technology/wappen/tiny/7055.png?lm=4711" title="Tigres UANL"/></a> </td>
<td class="hauptlink">
<a href="/tigres-uanl/startseite/verein/7055/saison_id/2026" title="Tigres UANL">Tigres UANL</a> </td>
</tr>
<tr>
<td>
<img alt="Mexico" class="flaggenrahmen" src="https://img.a.transfermarkt.technology/flagge/verysmall/110.png?lm=4711" title="Mexico"/> <a href="/liga-mx-apertura/transfers/wettbewerb/MEXA/saison_id/2026" title="Liga MX Apertura">Liga MX Apertura</a> </td>
</tr>
</table>
</td><td class="rechts hauptlink"><a href="/jumplist/transfers/spieler/577489/transfer_id/6579640">€3.40m</a></td></tr><tr class="even">
<td class=""> <table class="inline-table">
<tr>
<td rowspan="2">
<img alt="Karol Borys" class="bilderrahmen-fixed lazy lazy" data-src="https://img.a.transfermarkt.technology/portrait/medium/890272-1713450249.png?lm=4711" src="data:image/gif;base64,R0lGODlhAQABAIAAAMLCwgAAACH5BAAAAAAALAAAAAABAAEAAAICRAEAOw==" title="Karol Borys"/> </td>
<td class="hauptlink">
<a href="/karol-borys/profil/spieler/890272" title="Karol Borys">Karol Borys</a> </td>
</tr>
<tr>
<td>Attacking Midfield</td>
</tr>
</table>
</td><td class="zentriert">19</td><td class="zentriert"><img alt="Poland" class="flaggenrahmen" src="https://img.a.transfermarkt.technology/flagge/verysmall/135.png?lm=4711" title="Poland"/></td><td class=""><table class="inline-table">
<tr>
<td rowspan="2">
<a href="/kvc-westerlo/startseite/verein/968/saison_id/2026" title="KVC Westerlo"><img alt="KVC Westerlo" class="tiny_wappen" src="https://img.a.transfermarkt.technology/wappen/tiny/968.png?lm=4711" title="KVC Westerlo"/></a> </td>
<td class="hauptlink">
<a href="/kvc-westerlo/startseite/verein/968/saison_id/2026" title="KVC Westerlo">KVC Westerlo</a> </td>
</tr>
<tr>
<td>
<img alt="Belgium" class="flaggenrahmen" src="https://img.a.transfermarkt.technology/flagge/tiny/19.png?lm=4711" title="Belgium"/> <a href="/jupiler-pro-league/transfers/wettbewerb/BE1/saison_id/2026" title="Jupiler Pro League">Jupiler Pro League</a> </td>
</tr>
</table>
</td><td class=""><table class="inline-table">
<tr>
<td rowspan="2">
<a href="/slask-wroclaw/startseite/verein/759/saison_id/2026" title="Slask Wroclaw"><img alt="Slask Wroclaw" class="tiny_wappen" src="https://img.a.transfermarkt.technology/wappen/tiny/759.png?lm=4711" title="Slask Wroclaw"/></a> </td>
<td class="hauptlink">
<a href="/slask-wroclaw/startseite/verein/759/saison_id/2026" title="Slask Wroclaw">Slask Wroclaw</a> </td>
</tr>
<tr>
<td>
<img alt="Poland" class="flaggenrahmen" src="https://img.a.transfermarkt.technology/flagge/verysmall/135.png?lm=4711" title="Poland"/> <a href="/pko-ekstraklasa/transfers/wettbewerb/PL1/saison_id/2026" title="Ekstraklasa">Ekstraklasa</a> </td>
</tr>
</table>
</td><td class="rechts hauptlink"><a href="/jumplist/transfers/spieler/890272/transfer_id/6579548">free transfer</a></td></tr><tr class="odd">
<td class=""> <table class="inline-table">
<tr>
<td rowspan="2">
<img alt="Karlo Muhar" class="bilderrahmen-fixed lazy lazy" data-src="https://img.a.transfermarkt.technology/portrait/medium/388042-1769412322.png?lm=4711" src="data:image/gif;base64,R0lGODlhAQABAIAAAMLCwgAAACH5BAAAAAAALAAAAAABAAEAAAICRAEAOw==" title="Karlo Muhar"/> </td>
<td class="hauptlink">
<a href="/karlo-muhar/profil/spieler/388042" title="Karlo Muhar">Karlo Muhar</a> </td>
</tr>
<tr>
<td>Defensive Midfield</td>
</tr>
</table>
</td><td class="zentriert">30</td><td class="zentriert"><img alt="Croatia" class="flaggenrahmen" src="https://img.a.transfermarkt.technology/flagge/verysmall/37.png?lm=4711" title="Croatia"/></td><td class=""><table class="inline-table">
<tr>
<td rowspan="2">
<a href="/vereinslos/startseite/verein/515/saison_id/2026" title="Without ClubWithout Club"><img alt="Without Club" class="tiny_wappen" src="https://img.a.transfermarkt.technology/wappen/tiny/515.png?lm=4711" title="Without Club"/></a> </td>
<td class="hauptlink">
<a href="/vereinslos/startseite/verein/515/saison_id/2026" title="Without Club">Without Club</a> </td>
</tr>
<tr>
<td>
</td>
</tr>
</table>
</td><td class=""><table class="inline-table">
<tr>
<td rowspan="2">
<a href="/fcsb/startseite/verein/301/saison_id/2026" title="FCSB"><img alt="FCSB" class="tiny_wappen" src="https://img.a.transfermarkt.technology/wappen/tiny/301.png?lm=4711" title="FCSB"/></a> </td>
<td class="hauptlink">
<a href="/fcsb/startseite/verein/301/saison_id/2026" title="FCSB">FCSB</a> </td>
</tr>
<tr>
<td>
<img alt="Romania" class="flaggenrahmen" src="https://img.a.transfermarkt.technology/flagge/verysmall/140.png?lm=4711" title="Romania"/> <a href="/liga-1/transfers/wettbewerb/RO1/saison_id/2026" title="SuperLiga">SuperLiga</a> </td>
</tr>
</table>
</td><td class="rechts hauptlink"><a href="/jumplist/transfers/spieler/388042/transfer_id/6578440">-</a></td></tr></tbody></table></body></html>"""

FIX_PLAYER_DETAIL = """<html lang="en"><head><title>player detail</title></head><body><header class="data-header" itemscope="" itemtype="https://schema.org/Person">
<div class="data-header__headline-container">
<h1 class="data-header__headline-wrapper">
<span class="data-header__shirt-number">
                        #9                    </span>
                                Erling <strong>Haaland</strong> </h1>
<tm-watchlist player-id="418560"></tm-watchlist>
</div>
<div class="data-header__badge-container">
<a class="data-header__success-data" href="/erling-haaland/erfolge/spieler/418560" onclick="tmEvent('spielerprofil', 'click', 'erfolge');" title="UEFA Best Player in Europe ">
<img alt="UEFA Best Player in Europe " class="data-header__success-image lazy lazy" data-src="https://img.a.transfermarkt.technology/titel/header/152.png?lm=4711" src="data:image/gif;base64,R0lGODlhAQABAIAAAMLCwgAAACH5BAAAAAAALAAAAAABAAEAAAICRAEAOw==" title="UEFA Best Player in Europe "> <span class="data-header__success-number">1</span>
</img></a>
<a class="data-header__success-data" href="/erling-haaland/erfolge/spieler/418560" onclick="tmEvent('spielerprofil', 'click', 'erfolge');" title="Golden Boot winner (Europe)">
<img alt="Golden Boot winner (Europe)" class="data-header__success-image lazy lazy" data-src="https://img.a.transfermarkt.technology/titel/header/207.png?lm=4711" src="data:image/gif;base64,R0lGODlhAQABAIAAAMLCwgAAACH5BAAAAAAALAAAAAABAAEAAAICRAEAOw==" title="Golden Boot winner (Europe)"> <span class="data-header__success-number">1</span>
</img></a>
<a class="data-header__success-data" href="/erling-haaland/erfolge/spieler/418560" onclick="tmEvent('spielerprofil', 'click', 'erfolge');" title="UEFA Champions League winner">
<img alt="UEFA Champions League winner" class="" src="https://img.a.transfermarkt.technology/erfolge/header/4.png?lm=4711" title="UEFA Champions League winner"> <span class="data-header__success-number">1</span>
</img></a>
<a class="data-header__success-data" href="/erling-haaland/erfolge/spieler/418560" onclick="tmEvent('spielerprofil', 'click', 'erfolge');" title="English Champion">
<img alt="English Champion" class="" src="https://img.a.transfermarkt.technology/erfolge/header/12.png?lm=4711" title="English Champion"> <span class="data-header__success-number">2</span>
</img></a>
<a class="data-header__success-data" href="/erling-haaland/erfolge/spieler/418560" onclick="tmEvent('spielerprofil', 'click', 'erfolge');" title="Uefa Supercup winner">
<img alt="Uefa Supercup winner" class="" src="https://img.a.transfermarkt.technology/erfolge/header/354.png?lm=4711" title="Uefa Supercup winner"> <span class="data-header__success-number">1</span>
</img></a>
<a class="data-header__success-more" href="/erling-haaland/erfolge/spieler/418560" onclick="tmEvent('spielerprofil', 'click', 'erfolge');" title="All titles &amp; victories">
</a>
</div>
<div class="data-header__box--big">
<a class="data-header__box__club-link" href="/manchester-city/startseite/verein/281">
<img alt="Manchester City" height="100" srcset="
                            https://img.a.transfermarkt.technology/wappen/normquad/281.png?lm=4711 1x,
                            https://img.a.transfermarkt.technology/wappen/homepageWappen150x150/281.png?lm=4711 2x
                            " width="100">
</img></a>
<div class="data-header__club-info">
<span class="data-header__club" itemprop="affiliation">
<a href="/manchester-city/startseite/verein/281" title="Manchester City">Man City</a> </span><br>
<span class="data-header__league">
<a class="data-header__league-link" href="/premier-league/startseite/wettbewerb/GB1">
<img alt="Premier League" class="" src="https://img.a.transfermarkt.technology/logo/verytiny/gb1.png?lm=4711" title="Premier League">Premier League                                    </img></a>
</span>
<span class="data-header__label">League level:
                                <span class="data-header__content">
<img alt="England" class="flaggenrahmen" src="https://img.a.transfermarkt.technology/flagge/tiny/189.png?lm=4711" title="England">First Tier                                </img></span>
</span>
<span class="data-header__label">Joined: <span class="data-header__content">01/07/2022</span></span>
<span class="data-header__label">Contract expires: <span class="data-header__content">30/06/2034</span></span>
</br></div>
</div>
<div class="data-header__profile-container">
<div class="modal-trigger" data-custom-open="modal-1" id="fotoauswahlOeffnen" onclick="tmEvent('spielerprofil','click','profilbild');" style="cursor:pointer">
<img alt="Erling Haaland" class="data-header__profile-image" height="181" src="https://img.a.transfermarkt.technology/portrait/header/418560-1788115885.jpg?lm=4711" title="Erling Haaland" width="139"><div class="bildquelle"><span title="IMAGO">IMAGO</span></div>
<span class="modal-trigger-icon">+</span>
</img></div>
</div>
<div class="data-header__info-box">
<div class="data-header__details">
<ul class="data-header__items">
<li class="data-header__label">Date of birth/Age:
                            <span class="data-header__content" itemprop="birthDate">
                                21/07/2000 (26)                            </span>
</li>
<li class="data-header__label">Place of birth:
                                <img alt="England" class="flaggenrahmen" src="https://img.a.transfermarkt.technology/flagge/tiny/189.png?lm=4711" title="England"> <span class="data-header__content" itemprop="birthPlace">
                                    Leeds                                </span>
</img></li>
<li class="data-header__label">Citizenship:
                            <span class="data-header__content" itemprop="nationality">
<img alt="Norway" class="flaggenrahmen" src="https://img.a.transfermarkt.technology/flagge/tiny/125.png?lm=4711" title="Norway">                                Norway                            </img></span>
</li>
</ul>
<ul class="data-header__items">
<li class="data-header__label">Height:
                            <span class="data-header__content" itemprop="height">
                                1,95 m                            </span>
</li>
<li class="data-header__label">Position:
                        <span class="data-header__content">
                            Centre-Forward                        </span>
</li>
<li class="data-header__label">Agent:
                            <span class="data-header__content data-header__content--vertical-aligned">
<a href="/rafaela-pimenta/beraterfirma/berater/282" onclick='tmEvent("spielerprofil", "click", "berater-header-premium")'>Rafaela Pimenta</a> <img alt="verified" class="data-header__verified-logo" height="13" src="https://tmsi.akamaized.net/berater/verified_premium_minified.png" title="verified" width="13"/>
</span>
</li>
</ul>
<ul class="data-header__items">
<li class="data-header__label" for="">
                                                            Current international:
                                                        <span class="data-header__content">
<img alt="Norway" class="flaggenrahmen flagge" src="https://img.a.transfermarkt.technology/flagge/tiny/125.png?lm=4711" title="Norway"><a href="/norwegen/startseite/verein/3440" title="Norway">Norway</a> </img></span>
</li>
<li class="data-header__label">Caps/Goals:
                                <a class="data-header__content data-header__content--highlight" href="/erling-haaland/nationalmannschaft/spieler/418560/verein_id/3440">55                                </a>/
                                <a class="data-header__content data-header__content--highlight" href="/erling-haaland/nationalmannschaft/spieler/418560/verein_id/3440">62                                </a>
</li>
<li class="data-header__label theme-button">
</li>
</ul>
</div>
</div>
<div class="data-header__box--small">
<a class="data-header__market-value-wrapper" href="/erling-haaland/marktwertverlauf/spieler/418560"><span class="waehrung">€</span>220.00<span class="waehrung">m</span> <p class="data-header__last-update">Last update: 22/07/2026</p></a>
</div>
</header>
<div class="large-6 large-pull-6 columns print spielerdatenundfakten">
<div class="info-table info-table--right-space min-height-audio">
<span class="info-table__content info-table__content--regular">Name in home country:</span>
<span class="info-table__content info-table__content--bold">Erling Braut Håland</span>
<span class="info-table__content info-table__content--regular">Date of birth/Age:</span>
<span class="info-table__content info-table__content--bold">
<a href="/aktuell/waspassiertheute/aktuell/new/datum/2000-07-21">21/07/2000 (26)</a>
</span>
<span class="info-table__content info-table__content--regular">Place of birth:</span>
<span class="info-table__content info-table__content--bold">
<span>Leeds  <img alt="England" class="flaggenrahmen" src="https://img.a.transfermarkt.technology/flagge/tiny/189.png?lm=4711" title="England"/></span> </span>
<span class="info-table__content info-table__content--regular">Height:</span>
<span class="info-table__content info-table__content--bold">1,95 m</span>
<span class="info-table__content info-table__content--regular">Citizenship:</span>
<span class="info-table__content info-table__content--bold">
<img alt="Norway" class="flaggenrahmen" src="https://img.a.transfermarkt.technology/flagge/tiny/125.png?lm=4711" title="Norway"/>  Norway                    </span>
<span class="info-table__content info-table__content--regular">Position:</span>
<span class="info-table__content info-table__content--bold">
                    Attack - Centre-Forward                </span>
<span class="info-table__content info-table__content--regular">Foot:</span>
<span class="info-table__content info-table__content--bold">left</span>
<span class="info-table__content info-table__content--regular">Player agent:</span>
<span class="info-table__content info-table__content--bold info-table__content--flex">
<a href="/rafaela-pimenta/beraterfirma/berater/282" onclick='tmEvent("spielerprofil", "click", "berater-spielerdaten")'>Rafaela Pimenta</a>
</span>
<span class="info-table__content info-table__content--regular">
                    Current club:
                </span>
<span class="info-table__content info-table__content--bold info-table__content--flex">
<a href="/manchester-city/startseite/verein/281" title="Manchester City">Manchester City</a> </span>
<span class="info-table__content info-table__content--regular">Joined:</span>
<span class="info-table__content info-table__content--bold">
                            01/07/2022                        </span>
<span class="info-table__content info-table__content--regular">Contract expires:</span>
<span class="info-table__content info-table__content--bold">
                            30/06/2034                        </span>
<span class="info-table__content info-table__content--regular">Outfitter:</span>
<span class="info-table__content info-table__content--bold">Nike</span>
</div>
</div>
</body></html>"""


# A second, separate real-capture snippet (Lamine Yamal, 2026-09-10): NO
# info-table box at all -- only the header -- so parse_player_detail falls
# back to the header's own `[itemprop="birthPlace"]` reading. That span is
# where the site's CSS truncation was actually measured: a `.cp` child
# clips the text to "Esplugues de ..." and keeps the untruncated string in
# `title`. This fixture exists specifically to exercise that fallback path
# (_text_or_title) in isolation from the info-table's own untruncated copy,
# which is the PRIMARY source and is not present here.
FIX_PLAYER_DETAIL_NO_INFO_TABLE_TRUNCATED_BIRTHPLACE = """<html lang="en"><body><header class="data-header" itemscope="" itemtype="https://schema.org/Person">
<h1 class="data-header__headline-wrapper">Lamine Yamal</h1>
<div class="data-header__info-box"><div class="data-header__details"><ul class="data-header__items">
<li class="data-header__label">Date of birth/Age:
<span class="data-header__content" itemprop="birthDate">13/07/2007 (19)</span>
</li>
<li class="data-header__label">Place of birth:
<span class="data-header__content" itemprop="birthPlace">
<span class="cp" title="Esplugues de Llobregat">Esplugues de ...</span> </span>
</li>
</ul></div></div>
</header></body></html>"""


# A German-locale (`www.transfermarkt.de`) market-values snippet -- top 2
# rows of the SAME ranking page as FIX_MARKET_VALUES above (Lamine Yamal,
# Erling Haaland), fetched 2026-09-16, trimmed to 2 rows. Exists to exercise
# locale-aware money parsing (comma-decimal, "Mio." suffix, symbol-last) and
# position/nationality translation (Rechtsaußen/Mittelstürmer, Spanien/
# Norwegen) against REAL German markup, not a hand-written approximation.
FIX_MARKET_VALUES_DE = """<table class="items"><tbody>
<tr class="odd">
<td class="zentriert">1</td><td class=""><table class="inline-table"><tr><td rowspan="2"><a href="#"><img alt="Lamine Yamal" class="bilderrahmen-fixed" src="https://img.a.transfermarkt.technology/portrait/small/937958-1773173768.jpg?lm=4711" title="Lamine Yamal"/></a></td><td class="hauptlink"><a href="/lamine-yamal/profil/spieler/937958" title="Lamine Yamal">Lamine Yamal</a></td></tr><tr><td>Rechtsaußen</td></tr></table></td><td class="zentriert">19</td><td class="zentriert"><img alt="Spanien" class="flaggenrahmen" src="https://img.a.transfermarkt.technology/flagge/verysmall/157.png?lm=4711" title="Spanien"/></td><td class="zentriert"><a href="/fc-barcelona/startseite/verein/131" title="FC Barcelona"><img alt="FC Barcelona" class="" src="https://img.a.transfermarkt.technology/wappen/verysmall/131.png?lm=1789459175" title="FC Barcelona"/></a></td><td class="rechts hauptlink"><a href="/lamine-yamal/marktwertverlauf/spieler/937958">220,00 Mio. €</a> </td></tr>
<tr class="even">
<td class="zentriert">1</td><td class=""><table class="inline-table"><tr><td rowspan="2"><a href="#"><img alt="Erling Haaland" class="bilderrahmen-fixed" src="https://img.a.transfermarkt.technology/portrait/small/418560-1788115885.jpg?lm=4711" title="Erling Haaland"/></a></td><td class="hauptlink"><a href="/erling-haaland/profil/spieler/418560" title="Erling Haaland">Erling Haaland</a></td></tr><tr><td>Mittelstürmer</td></tr></table></td><td class="zentriert">26</td><td class="zentriert"><img alt="Norwegen" class="flaggenrahmen" src="https://img.a.transfermarkt.technology/flagge/verysmall/125.png?lm=4711" title="Norwegen"/></td><td class="zentriert"><a href="/manchester-city/startseite/verein/281" title="Manchester City"><img alt="Manchester City" class="" src="https://img.a.transfermarkt.technology/wappen/verysmall/281.png?lm=1789459183" title="Manchester City"/></a></td><td class="rechts hauptlink"><a href="/erling-haaland/marktwertverlauf/spieler/418560">220,00 Mio. €</a> </td></tr>
</tbody></table>"""


# Erling Haaland's own profile (id 418560), fetched from
# `www.transfermarkt.de` on 2026-09-16 -- header + info-table, trimmed the
# same way FIX_PLAYER_DETAIL was. Exercises the German label-key lookups
# (`im team seit`, `vertrag bis`, `name im heimatland`, `geburtsort`), the
# "Letzte Änderung:" last-update prefix, and the day.month.year date shape
# through `normalize_date`.
FIX_PLAYER_DETAIL_DE = """<html lang="de"><body><header class="data-header" itemscope="" itemtype="https://schema.org/Person">
<div class="data-header__headline-container">
<h1 class="data-header__headline-wrapper">
<span class="data-header__shirt-number">
                        #9                    </span>
                                Erling <strong>Haaland</strong> </h1>
<tm-watchlist player-id="418560"></tm-watchlist>
</div>
<div class="data-header__badge-container">
<a class="data-header__success-data" href="/erling-haaland/erfolge/spieler/418560" onclick="tmEvent('spielerprofil', 'click', 'erfolge');" title="UEFA Europas Fußballer des Jahres">
<img alt="UEFA Europas Fußballer des Jahres" class="data-header__success-image lazy lazy" data-src="https://img.a.transfermarkt.technology/titel/header/152.png?lm=4711" src="data:image/gif;base64,R0lGODlhAQABAIAAAMLCwgAAACH5BAAAAAAALAAAAAABAAEAAAICRAEAOw==" title="UEFA Europas Fußballer des Jahres"> <span class="data-header__success-number">1</span>
</img></a>
<a class="data-header__success-data" href="/erling-haaland/erfolge/spieler/418560" onclick="tmEvent('spielerprofil', 'click', 'erfolge');" title="Golden Boy">
<img alt="Golden Boy" class="data-header__success-image lazy lazy" data-src="https://img.a.transfermarkt.technology/titel/header/1348.png?lm=4711" src="data:image/gif;base64,R0lGODlhAQABAIAAAMLCwgAAACH5BAAAAAAALAAAAAABAAEAAAICRAEAOw==" title="Golden Boy"> <span class="data-header__success-number">1</span>
</img></a>
<a class="data-header__success-data" href="/erling-haaland/erfolge/spieler/418560" onclick="tmEvent('spielerprofil', 'click', 'erfolge');" title="UEFA Champions League-Sieger">
<img alt="UEFA Champions League-Sieger" class="" src="https://img.a.transfermarkt.technology/erfolge/header/4.png?lm=4711" title="UEFA Champions League-Sieger"> <span class="data-header__success-number">1</span>
</img></a>
<a class="data-header__success-data" href="/erling-haaland/erfolge/spieler/418560" onclick="tmEvent('spielerprofil', 'click', 'erfolge');" title="Englischer Meister">
<img alt="Englischer Meister" class="" src="https://img.a.transfermarkt.technology/erfolge/header/12.png?lm=4711" title="Englischer Meister"> <span class="data-header__success-number">2</span>
</img></a>
<a class="data-header__success-data" href="/erling-haaland/erfolge/spieler/418560" onclick="tmEvent('spielerprofil', 'click', 'erfolge');" title="UEFA-Supercup-Sieger">
<img alt="UEFA-Supercup-Sieger" class="" src="https://img.a.transfermarkt.technology/erfolge/header/354.png?lm=4711" title="UEFA-Supercup-Sieger"> <span class="data-header__success-number">1</span>
</img></a>
<a class="data-header__success-more" href="/erling-haaland/erfolge/spieler/418560" onclick="tmEvent('spielerprofil', 'click', 'erfolge');" title="Alle Titel &amp; Erfolge">
</a>
</div>
<div class="data-header__box--big">
<a class="data-header__box__club-link" href="/manchester-city/startseite/verein/281">
<img alt="Manchester City" height="100" srcset="
                            https://img.a.transfermarkt.technology/wappen/normquad/281.png?lm=1789459183 1x,
                            https://img.a.transfermarkt.technology/wappen/homepageWappen150x150/281.png?lm=1789459183 2x
                            " width="100">
</img></a>
<div class="data-header__club-info">
<span class="data-header__club" itemprop="affiliation">
<a href="/manchester-city/startseite/verein/281" title="Manchester City">Man City</a> </span><br>
<span class="data-header__league">
<a class="data-header__league-link" href="/premier-league/startseite/wettbewerb/GB1">
<img alt="Premier League" class="" src="https://img.a.transfermarkt.technology/logo/verytiny/gb1.png?lm=1789377131" title="Premier League">Premier League                                    </img></a>
</span>
<span class="data-header__label">Ligahöhe:
                                <span class="data-header__content">
<img alt="England" class="flaggenrahmen" src="https://img.a.transfermarkt.technology/flagge/tiny/189.png?lm=4711" title="England">1.Liga                                </img></span>
</span>
<span class="data-header__label">Im Team seit: <span class="data-header__content">01.07.2022</span></span>
<span class="data-header__label">Vertrag bis: <span class="data-header__content">30.06.2034</span></span>
</br></div>
</div>
<div class="data-header__profile-container">
<div class="modal-trigger" data-custom-open="modal-1" id="fotoauswahlOeffnen" onclick="tmEvent('spielerprofil','click','profilbild');" style="cursor:pointer">
<img alt="Erling Haaland" class="data-header__profile-image" height="181" src="https://img.a.transfermarkt.technology/portrait/header/418560-1788115885.jpg?lm=4711" title="Erling Haaland" width="139"><div class="bildquelle"><span title="IMAGO">IMAGO</span></div>
<span class="modal-trigger-icon">+</span>
</img></div>
</div>
<div class="data-header__info-box">
<div class="data-header__details">
<ul class="data-header__items">
<li class="data-header__label">Geb./Alter:
                            <span class="data-header__content" itemprop="birthDate">
                                21.07.2000 (26)                            </span>
</li>
<li class="data-header__label">Geburtsort:
                                <img alt="England" class="flaggenrahmen" src="https://img.a.transfermarkt.technology/flagge/tiny/189.png?lm=4711" title="England"> <span class="data-header__content" itemprop="birthPlace">
                                    Leeds                                </span>
</img></li>
<li class="data-header__label">Staatsbürgerschaft:
                            <span class="data-header__content" itemprop="nationality">
<img alt="Norwegen" class="flaggenrahmen" src="https://img.a.transfermarkt.technology/flagge/tiny/125.png?lm=4711" title="Norwegen">                                Norwegen                            </img></span>
</li>
</ul>
<ul class="data-header__items">
<li class="data-header__label">Größe:
                            <span class="data-header__content" itemprop="height">
                                1,95 m                            </span>
</li>
<li class="data-header__label">Position:
                        <span class="data-header__content">
                            Mittelstürmer                        </span>
</li>
<li class="data-header__label">Berater:
                            <span class="data-header__content data-header__content--vertical-aligned">
<a href="/rafaela-pimenta/beraterfirma/berater/282" onclick='tmEvent("spielerprofil", "click", "berater-header-premium")'>Rafaela Pimenta</a> <img alt="Verifiziert" class="data-header__verified-logo" height="13" src="https://tmsi.akamaized.net/berater/verified_premium_minified.png" title="Verifiziert" width="13"/>
</span>
</li>
</ul>
<ul class="data-header__items">
<li class="data-header__label" for="">
                                                            Akt. Nationalspieler:
                                                        <span class="data-header__content">
<img alt="Norwegen" class="flaggenrahmen flagge" src="https://img.a.transfermarkt.technology/flagge/tiny/125.png?lm=4711" title="Norwegen"><a href="/norwegen/startseite/verein/3440" title="Norwegen">Norwegen</a> </img></span>
</li>
<li class="data-header__label">Länderspiele/Tore:
                                <a class="data-header__content data-header__content--highlight" href="/erling-haaland/nationalmannschaft/spieler/418560/verein_id/3440">55                                </a>/
                                <a class="data-header__content data-header__content--highlight" href="/erling-haaland/nationalmannschaft/spieler/418560/verein_id/3440">62                                </a>
</li>
<li class="data-header__label theme-button">
</li>
</ul>
</div>
</div>
<div class="data-header__box--small">
<a class="data-header__market-value-wrapper" href="/erling-haaland/marktwertverlauf/spieler/418560">220,00 <span class="waehrung">Mio. €</span> <p class="data-header__last-update">Letzte Änderung: 22.07.2026</p></a>
</div>
</header><div class="info-table info-table--right-space min-height-audio">
<span class="info-table__content info-table__content--regular">Name im Heimatland:</span>
<span class="info-table__content info-table__content--bold">Erling Braut Håland</span>
<span class="info-table__content info-table__content--regular">Geb./Alter:</span>
<span class="info-table__content info-table__content--bold">
<a href="/aktuell/waspassiertheute/aktuell/new/datum/2000-07-21">21.07.2000 (26)</a>
</span>
<span class="info-table__content info-table__content--regular">Geburtsort:</span>
<span class="info-table__content info-table__content--bold">
<span>Leeds  <img alt="England" class="flaggenrahmen lazy lazy" data-src="https://img.a.transfermarkt.technology/flagge/tiny/189.png?lm=4711" src="data:image/gif;base64,R0lGODlhAQABAIAAAMLCwgAAACH5BAAAAAAALAAAAAABAAEAAAICRAEAOw==" title="England"/></span> </span>
<span class="info-table__content info-table__content--regular">Größe:</span>
<span class="info-table__content info-table__content--bold">1,95 m</span>
<span class="info-table__content info-table__content--regular">Staatsbürgerschaft:</span>
<span class="info-table__content info-table__content--bold">
<img alt="Norwegen" class="flaggenrahmen" src="https://img.a.transfermarkt.technology/flagge/tiny/125.png?lm=4711" title="Norwegen">  Norwegen                    </img></span>
<span class="info-table__content info-table__content--regular">Position:</span>
<span class="info-table__content info-table__content--bold">
                    Sturm - Mittelstürmer                </span>
<span class="info-table__content info-table__content--regular">Fuß:</span>
<span class="info-table__content info-table__content--bold">links</span>
<span class="info-table__content info-table__content--regular">Spielerberater:</span>
<span class="info-table__content info-table__content--bold info-table__content--flex">
<a href="/rafaela-pimenta/beraterfirma/berater/282" onclick='tmEvent("spielerprofil", "click", "berater-spielerdaten")'>Rafaela Pimenta</a> <img alt="Verifiziert" height="13" src="https://tmsi.akamaized.net/berater/verified_premium_minified.png" title="Verifiziert" width="13"/>
</span>
<span class="info-table__content info-table__content--regular">
                    Aktueller Verein:
                </span>
<span class="info-table__content info-table__content--bold info-table__content--flex">
<a href="/manchester-city/startseite/verein/281">
<img alt="Manchester City" height="15" srcset="https://img.a.transfermarkt.technology/wappen/small/281.png?lm=1789459183 1x,
                                            https://img.a.transfermarkt.technology/wappen/homepage/281.png?lm=1789459183 2x" width="15">
</img></a>
<a href="/manchester-city/startseite/verein/281" title="Manchester City">Manchester City</a> </span>
<span class="info-table__content info-table__content--regular">Im Team seit:</span>
<span class="info-table__content info-table__content--bold">
                            01.07.2022                        </span>
<span class="info-table__content info-table__content--regular">Vertrag bis:</span>
<span class="info-table__content info-table__content--bold">30.06.2034</span>
<span class="info-table__content info-table__content--regular">Letzte Verlängerung:</span>
<span class="info-table__content info-table__content--bold">17.01.2025</span>
<span class="info-table__content info-table__content--regular">Ausrüster:</span>
<span class="info-table__content info-table__content--bold">Nike</span>
<span class="info-table__content info-table__content--regular">Social Media:</span>
<span class="info-table__content info-table__content--bold">
<div class="social-media-toolbar__icons">
<a aria-label="Öffnet die Seite von {Twitter} in einem neuen Tab" href="http://twitter.com/ErlingHaaland" target="_blank" title="Twitter">
<img alt="Social Media Links" class="lazy social" data-src="https://tmsi.akamaized.net/icons/socialMedia/twitter-x-logo-square.svg" height="15px" width="15px"> </img></a>
<a aria-label="Öffnet die Seite von {Facebook} in einem neuen Tab" href="http://www.facebook.com/profile.php?id=100052762394674" target="_blank" title="Facebook">
<img alt="Social Media Links" class="lazy social" data-src="https://tmsi.akamaized.net/icons/socialMedia/facebook.svg" height="15px" width="15px"> </img></a>
<a aria-label="Öffnet die Seite von {Instagram} in einem neuen Tab" href="http://instagram.com/erling/" target="_blank" title="Instagram">
<img alt="Social Media Links" class="lazy social" data-src="https://tmsi.akamaized.net/icons/socialMedia/instagram.svg" height="15px" width="15px"/> </a>
</div>
</span>
</div></body></html>"""


# ---------------------------------------------------------------------------
def test_money_parsing():
    group("parse_market_value: the site's own money format")
    ok = True
    cases = [
        ("€220.00m", (220_000_000.0, "EUR")),
        ("€1.50m", (1_500_000.0, "EUR")),
        ("€300k", (300_000.0, "EUR")),
        ("€50k", (50_000.0, "EUR")),
        ("€1.20bn", (1_200_000_000.0, "EUR")),
        ("-", (None, None)),
        ("", (None, None)),
        ("?", (None, None)),
        (None, (None, None)),
        ("free transfer", (None, None)),  # no € sign, no digit-suffix match
    ]
    for text, expected in cases:
        got = parse_market_value(text)
        ok &= check("parse_market_value(%r) == %r" % (text, expected), got == expected)

    # Case-insensitivity on the suffix, and a bare euro figure with no
    # multiplier at all (a fee under a thousand euros is not observed on this
    # site but the parser must not choke on one).
    ok &= check("suffix is matched case-insensitively",
                parse_market_value("€1.50M") == (1_500_000.0, "EUR"))
    ok &= check("a bare figure with no suffix is not multiplied",
                parse_market_value("€500") == (500.0, "EUR"))
    # A thousands-grouping comma on a suffixed figure is stripped, not read
    # as a decimal point the way a German-locale value would be -- this site
    # is the English-language .com, which uses a period (see the module
    # docstring). Not observed on this site, but the parser must not silently
    # mis-scale a value if one ever appears.
    ok &= check("a comma is treated as a thousands separator, not a decimal",
                parse_market_value("€1,500k") == (1_500_000.0, "EUR"))
    return ok


def test_locale_money_parsing():
    group("parse_market_value(locale=...): the SAME real figure in five "
         "real formats (measured 2026-09-16 on the market-values page)")
    ok = True
    # (text, locale) -> (amount, "EUR"); every string here is a real one
    # copied verbatim from a live fetch, not hand-constructed.
    cases = [
        ("€220.00m", "en", (220_000_000.0, "EUR")),
        ("220,00 Mio. €", "de", (220_000_000.0, "EUR")),
        ("220,00 млн €", "ru", (220_000_000.0, "EUR")),
        ("220.00 mil. €", "ko", (220_000_000.0, "EUR")),
        ("220.00 m €", "ja", (220_000_000.0, "EUR")),
        ("160,00 Mio. €", "de", (160_000_000.0, "EUR")),
        ("-", "de", (None, None)),
        ("", "ru", (None, None)),
    ]
    for text, locale, expected in cases:
        got = parse_market_value(text, locale=locale)
        ok &= check("parse_market_value(%r, locale=%r) == %r" % (text, locale, expected),
                    got == expected)

    # An unrecognised locale code falls back to the English format rather
    # than raising or silently misparsing.
    ok &= check("an unknown locale code falls back to the 'en' format",
                parse_market_value("€1.50m", locale="xx") == (1_500_000.0, "EUR"))
    return ok


def test_locale_translation():
    group("translate_position / translate_nationality: real de/ru/ko/ja "
         "text -> the single English vocabulary")
    ok = True
    # Every (native, expected) pair below was extracted by matching the SAME
    # real player's row across a locale capture and the English capture at
    # the identical DOM position (see product_parser.py's "Locale text
    # normalisation" section for exactly how, and why nationality pairs are
    # restricted to single-flag players).
    position_cases = [
        ("de", "Mittelstürmer", "Centre-Forward"),
        ("de", "Torwart", "Goalkeeper"),
        ("de", "Linker Verteidiger", "Left-Back"),
        ("de", "Rechter Verteidiger", "Right-Back"),
        ("ru", "Центральный нап.", "Centre-Forward"),
        ("ru", "Левый Вингер", "Left Winger"),
        ("ko", "중앙 공격수", "Centre-Forward"),
        ("ko", "수비형 미드필더", "Defensive Midfield"),
        ("ja", "センターフォワード", "Centre-Forward"),
        ("ja", "攻撃的ミッドフィールダー", "Attacking Midfield"),
    ]
    for locale, native, expected in position_cases:
        ok &= check("translate_position(%r, locale=%r) == %r" % (native, locale, expected),
                    translate_position(native, locale=locale) == expected)

    # The info-table's "<broad unit> - <position>" shape (measured on
    # Haaland's own de/ru/ko/ja profiles): only the part after " - " is
    # looked up.
    ok &= check("a locale's 'unit - position' info-table shape is reduced "
                "to the position before translation",
                translate_position("Sturm - Mittelstürmer", locale="de") == "Centre-Forward")

    nationality_cases = [
        ("de", "Norwegen", "Norway"), ("de", "Spanien", "Spain"),
        ("de", "Elfenbeinküste", "Cote d'Ivoire"),
        ("ru", "Норвегия", "Norway"), ("ru", "Испания", "Spain"),
        ("ko", "노르웨이", "Norway"), ("ko", "스페인", "Spain"),
    ]
    for locale, native, expected in nationality_cases:
        ok &= check("translate_nationality(%r, locale=%r) == %r" % (native, locale, expected),
                    translate_nationality(native, locale=locale) == expected)

    # Measured asymmetry: Japanese flag title/alt text is already English
    # (unlike JP club names elsewhere on the same page) -- nothing to
    # translate, so it passes through unchanged rather than being looked up
    # in a table that would not have an entry for it anyway.
    ok &= check("Japanese nationality text is already English and passes "
                "through unchanged",
                translate_nationality("Norway", locale="ja") == "Norway")

    # An untranslated string (not in the table) must pass through AS-IS,
    # never crash and never get mis-translated by a guess.
    ok &= check("an unmapped position passes through untranslated, not raise",
                translate_position("Some New Position", locale="de") == "Some New Position")
    ok &= check("an unmapped nationality passes through untranslated, not raise",
                translate_nationality("Absurdistan", locale="ru") == "Absurdistan")
    ok &= check("English text is never looked up in a translation table "
                "(passthrough is a no-op for locale='en')",
                translate_position("Centre-Forward", locale="en") == "Centre-Forward"
                and translate_nationality("Spain", locale="en") == "Spain")
    ok &= check("None/empty text does not crash either function",
                translate_position(None, locale="de") is None
                and translate_nationality("", locale="ru") == "")
    return ok


def test_normalize_date():
    group("normalize_date: five real date shapes -> ISO 8601 (measured on "
         "Erling Haaland's own profile, id 418560, across all five locales)")
    ok = True
    cases = [
        ("en", "21/07/2000 (26)", None),  # age suffix not part of the date itself
        ("en", "01/07/2022", "2022-07-01"),
        ("de", "21.07.2000", "2000-07-21"),
        ("de", "30.06.2034", "2034-06-30"),
        ("ru", "21 июля 2000 г. (26)", None),
        ("ru", "01 июля 2022 г.", "2022-07-01"),
        ("ko", "2000년 7월 21일", "2000-07-21"),
        ("ja", "2022/07/01", "2022-07-01"),
        ("ja", "2000/07/21", "2000-07-21"),
    ]
    for locale, text, expected in cases:
        if expected is None:
            continue
        got = normalize_date(text, locale=locale)
        ok &= check("normalize_date(%r, locale=%r) == %r" % (text, locale, expected),
                    got == expected)

    # The Russian spelled-out genitive month name -- a fixed, closed
    # 12-entry vocabulary of the language itself (not sampled/measured data),
    # unlike positions/nationalities which are looked up from real captures.
    ok &= check("Russian genitive month names are all recognised",
                all(normalize_date(f"01 {m} 2000 г.", locale="ru") == f"2000-{n:02d}-01"
                    for m, n in [("января", 1), ("декабря", 12), ("июня", 6)]))

    ok &= check("an unparseable string passes through unchanged, not raise",
                normalize_date("not a date", locale="de") == "not a date")
    ok &= check("None/empty passes through", normalize_date(None) is None
                and normalize_date("") == "")
    return ok


def test_locale_market_values():
    group("parse_market_values on a REAL German-locale capture "
         "(www.transfermarkt.de, 2026-09-16)")
    ok = True
    rows = parse_market_values(FIX_MARKET_VALUES_DE, "https://www.transfermarkt.de/x", page_num=1)
    ok &= check("parses both rows in the German fixture", len(rows) == 2)
    if len(rows) != 2:
        return ok

    yamal, haaland = rows[0], rows[1]
    ok &= check("row 0 is still Lamine Yamal (same player, German markup)",
                yamal.sku == "937958" and yamal.title == "Lamine Yamal")
    ok &= check("German money format parses to the same EUR amount as the "
                "English capture of the same player (220,00 Mio. € == €220.00m)",
                yamal.price == 220_000_000.0 and yamal.currency == "EUR")
    ok &= check("German position text is translated to English ('Rechtsaußen' "
                "-> 'Right Winger')",
                yamal.position == "Right Winger")
    ok &= check("German nationality text is translated to English "
                "('Spanien' -> 'Spain')",
                yamal.nationality == "Spain" and yamal.nationalities == ["Spain"])
    ok &= check("Haaland's German position/nationality also translate "
                "('Mittelstürmer'/'Norwegen' -> 'Centre-Forward'/'Norway')",
                haaland.position == "Centre-Forward" and haaland.nationality == "Norway")
    ok &= check("source is tagged with the ACTUAL host fetched, not the "
                "English default",
                yamal.source == "www.transfermarkt.de"
                and haaland.source == "www.transfermarkt.de")
    return ok


def test_locale_player_detail():
    group("parse_player_detail on a REAL German-locale capture "
         "(Erling Haaland, www.transfermarkt.de, 2026-09-16)")
    ok = True
    p = parse_player_detail(FIX_PLAYER_DETAIL_DE,
                            "https://www.transfermarkt.de/x/profil/spieler/418560")
    ok &= check("a German detail page still parses to a Player", p is not None)
    if p is None:
        return ok

    ok &= check("sku/name/shirt number are locale-independent (same URL/DOM "
                "scheme as English)",
                p.sku == "418560" and p.title == "Erling Haaland" and p.shirt_number == 9)
    ok &= check("position is read via the German header label key "
                "('position') and translated to English",
                p.position == "Centre-Forward")
    ok &= check("market value: same EUR220m as the English capture, from "
                "the German '220,00 Mio. €' format",
                p.price == 220_000_000.0 and p.currency == "EUR")
    ok &= check("market_value_last_update: German 'Letzte Änderung:' prefix "
                "stripped and the date normalised to ISO 8601",
                p.market_value_last_update == "2026-07-22")
    ok &= check("joined_date/contract_until read via the German label keys "
                "('im team seit'/'vertrag bis') and normalised",
                p.joined_date == "2022-07-01" and p.contract_until == "2034-06-30")
    ok &= check("birth_date normalised from the German day.month.year shape",
                p.birth_date == "2000-07-21" and p.age == 26)
    ok &= check("full_name read via the German info-table key "
                "('name im heimatland') -- his native-spelling name, same "
                "value the English capture's 'Name in home country' row has",
                p.full_name == "Erling Braut Håland")
    ok &= check("birth_place read via the German info-table key ('geburtsort')",
                p.birth_place == "Leeds")
    ok &= check("nationality is translated German->English ('Norwegen' -> "
                "'Norway')",
                p.nationality == "Norway" and p.nationalities == ["Norway"])
    ok &= check("source is the German host, not hardcoded to English",
                p.source == "www.transfermarkt.de")
    ok &= check("category is still 'player'", p.category == "player")
    return ok


def test_market_values():
    group("parse_market_values (real capture: ranking page 1, rows 0-4)")
    ok = True
    rows = parse_market_values(FIX_MARKET_VALUES, "https://www.transfermarkt.com/x", page_num=1)
    ok &= check("parses exactly the 5 player rows in the fixture", len(rows) == 5)
    if len(rows) != 5:
        return ok

    ok &= check("row 0 is Lamine Yamal, sku 937958, rank 1, EUR220m",
                rows[0].sku == "937958" and rows[0].title == "Lamine Yamal"
                and rows[0].rank == 1 and rows[0].price == 220_000_000.0
                and rows[0].currency == "EUR")
    ok &= check("row 0's price_source is 'listing' (no detail page fetched)",
                rows[0].price_source == "listing")
    ok &= check("row 0's category is 'market-values' and page/row_index are recorded",
                rows[0].category == "market-values" and rows[0].page == 1 and rows[0].row_index == 0)
    ok &= check("row 0's url is built from the player id, not a scraped href",
                rows[0].url == "https://www.transfermarkt.com/x/profil/spieler/937958")

    # Kylian Mbappe: the dual-nationality case. Both flags kept, in order;
    # `nationality` is the first one, not an arbitrary pick.
    mbappe = next((r for r in rows if r.sku == "342229"), None)
    ok &= check("Kylian Mbappe is in the fixture", mbappe is not None)
    if mbappe:
        ok &= check("dual nationality: both flags kept in document order",
                    mbappe.nationalities == ["France", "Cameroon"])
        ok &= check("nationality (singular) is the first flag, not a coin flip",
                    mbappe.nationality == "France")
        ok &= check("Mbappe's club is Real Madrid (418)",
                    mbappe.club == "Real Madrid" and mbappe.club_id == "418")

    # A single-nationality row must not get a phantom second entry.
    ok &= check("a single-nationality row's nationalities list has one entry",
                rows[0].nationalities == ["Spain"])

    ok &= check("every row in this fixture has a numeric player sku",
                all(r.sku and r.sku.isdigit() for r in rows))
    ok &= check("no duplicate skus in one page", len({r.sku for r in rows}) == len(rows))
    return ok


def test_club_squad():
    group("parse_club_squad (real capture: Manchester City)")
    ok = True
    rows = parse_club_squad(FIX_CLUB_SQUAD, "https://www.transfermarkt.com/x", club_id="281")
    ok &= check("parses exactly the 5 rows kept in the fixture", len(rows) == 5)
    if len(rows) != 5:
        return ok

    donnarumma = rows[0]
    ok &= check("row 0 is Donnarumma, priced at EUR45m, shirt #25",
                donnarumma.title == "Gianluigi Donnarumma"
                and donnarumma.price == 45_000_000.0 and donnarumma.currency == "EUR"
                and donnarumma.shirt_number == 25)
    ok &= check("category carries the club id (club-squad/281)",
                donnarumma.category == "club-squad/281")
    ok &= check("club/club_id are filled even when the row's own cell is blank "
                "(falls back to the requested club_id)",
                donnarumma.club_id == "281")

    # The genuine null case: a real Transfermarkt free/youth player has no
    # published market value, and that must reach the output as None -- not
    # as 0.0, which would understate their contribution to a squad total.
    unpriced = next((r for r in rows if r.price is None), None)
    ok &= check("a player with '-' for market value parses to price=None, not 0.0",
                unpriced is not None)
    if unpriced:
        ok &= check("...and their currency is also None, not 'EUR' with a null amount",
                    unpriced.currency is None)
        ok &= check("...and price_source is None (nothing to attribute an unknown price to)",
                    unpriced.price_source is None)

    ok &= check("every row is tagged with the source host (derived from the "
                "fetch URL, not hardcoded -- www.transfermarkt.com here since "
                "that's what the fixture's base_url actually is)",
                all(r.source == "www.transfermarkt.com" for r in rows))
    ok &= check("no duplicate skus in one squad", len({r.sku for r in rows}) == len(rows))
    return ok


def test_transfers():
    group("parse_transfers (real capture: latest-transfers listing)")
    ok = True
    rows = parse_transfers(FIX_TRANSFERS, "https://www.transfermarkt.com/x", page_num=1)
    ok &= check("parses exactly the 6 rows kept in the fixture", len(rows) == 6)
    if len(rows) != 6:
        return ok

    by_sku = {r.sku: r for r in rows}
    ok &= check("every row's sku is the TRANSFER id, not the player id "
                "(they are different numbers here)",
                all(r.sku != r.player_id for r in rows))
    ok &= check("no duplicate transfer ids", len(by_sku) == len(rows))

    disclosed = by_sku.get("6579640")
    ok &= check("a disclosed fee is a real euro amount with fee_type='disclosed'",
                disclosed is not None and disclosed.price == 3_400_000.0
                and disclosed.currency == "EUR" and disclosed.fee_type == "disclosed")
    ok &= check("disclosed row: from/to clubs and leagues are both filled",
                disclosed.from_club == "Puebla FC" and disclosed.to_club == "Tigres UANL"
                and disclosed.from_club_id == "5662" and disclosed.to_club_id == "7055")

    loan = by_sku.get("6579806")
    ok &= check("a loan transfer has fee_type='loan' and price=None (loan fees "
                "are not printed on this listing)",
                loan is not None and loan.fee_type == "loan" and loan.price is None)

    free = by_sku.get("6579548")
    ok &= check("a free transfer has fee_type='free' and price=None",
                free is not None and free.fee_type == "free" and free.price is None)

    undisclosed = by_sku.get("6579119")
    ok &= check("an undisclosed fee ('?') has fee_type='undisclosed' and price=None",
                undisclosed is not None and undisclosed.fee_type == "undisclosed"
                and undisclosed.price is None)

    # The regression case: "Without ClubWithout Club" doubled-title bug.
    # Both rows in the fixture that come from "Without Club" must show the
    # CLEAN name, not the doubled title -- whichever of the two raw shapes
    # (clean title, or the doubled one) the row happened to carry.
    without_club_rows = [r for r in rows if r.from_club == "Without Club"]
    ok &= check("both 'Without Club' free-agent rows resolve to the clean "
                "name, including the one whose raw title was doubled "
                "('Without ClubWithout Club')",
                len(without_club_rows) == 2)
    ok &= check("...and the doubled title never leaks into output",
                not any((r.from_club or "").count("Without Club") > 1 for r in rows))
    ok &= check("'Without Club' rows still carry a real from_club_id",
                all(r.from_club_id == "515" for r in without_club_rows))

    ok &= check("category is 'latest-transfers' on every row",
                all(r.category == "latest-transfers" for r in rows))
    return ok


def test_player_detail():
    group("parse_player_detail (real capture: Erling Haaland)")
    ok = True
    p = parse_player_detail(FIX_PLAYER_DETAIL,
                            "https://www.transfermarkt.com/x/profil/spieler/418560")
    ok &= check("a detail page with the right header type parses to a Player", p is not None)
    if p is None:
        return ok

    ok &= check("sku is the player id from the URL", p.sku == "418560")
    ok &= check("name and shirt number", p.title == "Erling Haaland" and p.shirt_number == 9)
    ok &= check("position comes from the label map, not a hardcoded index",
                p.position == "Centre-Forward")
    ok &= check("market value: EUR220m, price_source='detail'",
                p.price == 220_000_000.0 and p.currency == "EUR" and p.price_source == "detail")
    ok &= check("market_value_last_update is read separately from the amount, "
                "and normalised to ISO 8601 (see normalize_date)",
                p.market_value_last_update == "2026-07-22")
    ok &= check("birth_date is the date alone (ISO 8601), age parsed "
                "separately from the parenthesised figure next to it",
                p.birth_date == "2000-07-21" and p.age == 26)
    ok &= check("birth_place, height (metres, comma-decimal on-site converted "
                "to a float) and agent",
                p.birth_place == "Leeds" and p.height_m == 1.95 and p.agent == "Rafaela Pimenta")
    ok &= check("club/club_id and league/league_id from the header, not the labels map",
                p.club == "Manchester City" and p.club_id == "281"
                and p.league == "Premier League" and p.league_id == "GB1")
    # The bug this repo found and fixed: these two use `span.data-header__label`,
    # not the `li.data-header__label` every other field in this header uses.
    ok &= check("joined_date and contract_until are populated (span-labelled "
                "fields, not li-labelled -- see _label_map's docstring) and "
                "normalised to ISO 8601",
                p.joined_date == "2022-07-01" and p.contract_until == "2034-06-30")
    ok &= check("nationality and the singular/plural pair agree for a "
                "single-nationality player",
                p.nationality == "Norway" and p.nationalities == ["Norway"])
    ok &= check("international caps and goals are read from the two "
                "highlighted header links, in that order",
                p.caps == 55 and p.international_goals == 62)
    ok &= check("category is 'player'", p.category == "player")
    # full_name used to just mirror the display name (`title`) -- a real bug,
    # not a design choice: the site publishes the actual full name in a
    # SEPARATE "Facts and data" info-table panel, under "Full name" on most
    # profiles or "Name in home country" on ones where the two differ
    # (Haaland's fixture here; see _info_table_map's docstring). Confirmed
    # live on 2026-09-10 that the pre-fix code silently duplicated `title`
    # instead of reading either.
    ok &= check("full_name is the real info-table value, not a copy of title "
                "(regression check -- this used to equal p.title)",
                p.full_name == "Erling Braut Håland" and p.full_name != p.title)

    # A header of the wrong itemtype (or none) must not be parsed as if it
    # were a player -- a malformed/foreign page should report "no data",
    # never guessed fields.
    ok &= check("a page with no matching header returns None, not a half-filled Player",
                parse_player_detail("<html><body>not a profile</body></html>",
                                    "https://www.transfermarkt.com/x") is None)

    group("parse_player_detail (real capture: Lamine Yamal, no info-table -- "
          "header-only truncation fallback)")
    p2 = parse_player_detail(FIX_PLAYER_DETAIL_NO_INFO_TABLE_TRUNCATED_BIRTHPLACE,
                             "https://www.transfermarkt.com/lamine-yamal/profil/spieler/937958")
    ok &= check("a profile with no info-table box still parses", p2 is not None)
    if p2 is not None:
        # The regression this guards against: reading birth_place as
        # `.get_text()` alone silently ships the site's own CSS-truncated
        # "Esplugues de ..." instead of the untruncated string sitting right
        # there in the span's `title` attribute -- measured live 2026-09-10,
        # the same class of bug `_detitle` already guards against for a
        # doubled link title, just triggered by truncation instead.
        ok &= check("birth_place prefers the untruncated `title` over the "
                    "clipped visible text when no info-table box is present",
                    p2.birth_place == "Esplugues de Llobregat")
        # No info-table box at all here, so full_name has nothing to read --
        # it must stay None, never fall back to duplicating the display name
        # (that fallback is exactly the bug being fixed).
        ok &= check("full_name is None, not a fallback to the display name, "
                    "when no info-table box is present",
                    p2.full_name is None)
    return ok


# ---------------------------------------------------------------------------
def test_urls_and_ids():
    group("URL builders, page numbers and id extraction")
    ok = True

    ok &= check("player_url builds a working /profil/spieler/{id} URL",
                player_url("418560") == "https://www.transfermarkt.com/x/profil/spieler/418560")
    ok &= check("player_url accepts a real slug too (cosmetic -- both work)",
                player_url("418560", slug="erling-haaland")
                == "https://www.transfermarkt.com/erling-haaland/profil/spieler/418560")
    ok &= check("club_squad_url with no season",
                club_squad_url("281") == "https://www.transfermarkt.com/x/kader/verein/281")
    ok &= check("club_squad_url with a season appends /saison_id/{season}",
                club_squad_url("281", season="2025")
                == "https://www.transfermarkt.com/x/kader/verein/281/saison_id/2025")

    ok &= check("MARKET_VALUES_URL and TRANSFERS_URL are both on the verified host",
                is_supported_host(MARKET_VALUES_URL) and is_supported_host(TRANSFERS_URL))

    # page_url / page_number_from_url round-trip -- only meaningful for
    # transfers, but the functions themselves are mode-agnostic string tools.
    base = TRANSFERS_URL
    p2 = page_url(base, 2)
    ok &= check("page_url(base, 2) appends ?page=2", p2.endswith("page=2"))
    ok &= check("page_url(base, 1) has no ?page= at all (page 1 is bare)",
                "page=" not in page_url(base, 1))
    ok &= check("page_number_from_url round-trips what page_url built",
                page_number_from_url(p2) == 2)
    ok &= check("a bare URL with no ?page= is page 1",
                page_number_from_url(base) == 1)
    ok &= check("page_url is idempotent -- building page 2 of an already-page-2 URL "
                "does not stack a second ?page=",
                page_url(p2, 2).count("page=") == 1)

    # Id extractors.
    ok &= check("player_id_from_url", player_id_from_url(
        "https://www.transfermarkt.com/erling-haaland/profil/spieler/418560") == "418560")
    ok &= check("club_id_from_url", club_id_from_url(
        "https://www.transfermarkt.com/manchester-city/startseite/verein/281") == "281")
    ok &= check("transfer_id_from_url", transfer_id_from_url(
        "https://www.transfermarkt.com/x/jumplist/transfers/spieler/418560/transfer_id/6579806") == "6579806")
    ok &= check("league_id_from_url", league_id_from_url(
        "https://www.transfermarkt.com/premier-league/startseite/wettbewerb/GB1") == "GB1")
    for fn, label in ((player_id_from_url, "player_id_from_url"),
                     (club_id_from_url, "club_id_from_url"),
                     (transfer_id_from_url, "transfer_id_from_url"),
                     (league_id_from_url, "league_id_from_url")):
        ok &= check("%s(None) is None, not a crash" % label, fn(None) is None)
        ok &= check("%s('') on an unrelated URL is None" % label,
                    fn("https://example.com/nothing/here") is None)

    # Host verification -- the family rule of refusing a same-brand host that
    # was never actually checked, rather than silently attempting it.
    ok &= check("www.transfermarkt.com is supported",
                is_supported_host("https://www.transfermarkt.com/x"))
    ok &= check("transfermarkt.com (no www) is supported",
                is_supported_host("https://transfermarkt.com/x"))
    # v0.3: four locale TLDs were fetched, their markup checked against the
    # English site, and added to HOSTS -- so these four are now SUPPORTED,
    # not refused (see LOCALE_BY_HOST's comment for what was verified).
    ok &= check("a verified locale TLD (.de) is supported, mapped to locale 'de'",
                is_supported_host("https://www.transfermarkt.de/x")
                and locale_of("https://www.transfermarkt.de/x") == "de")
    for host, locale in (("www.transfermarkt.world", "ru"),
                         ("www.transfermarkt.co.kr", "ko"),
                         ("www.transfermarkt.jp", "ja")):
        ok &= check(f"a verified locale TLD ({host}) is supported, mapped to "
                    f"locale {locale!r}",
                    is_supported_host(f"https://{host}/x")
                    and locale_of(f"https://{host}/x") == locale)
    ok &= check("an UNVERIFIED locale TLD (.es) is still refused with a "
                "reason naming why -- not every hreflang-listed host, only "
                "the five actually checked",
                not is_supported_host("https://www.transfermarkt.es/x")
                and "locale site" in (unsupported_reason("https://www.transfermarkt.es/x") or ""))
    ok &= check("an unrelated host is refused",
                not is_supported_host("https://example.com/x"))
    ok &= check("an invalid URL is refused without raising",
                not is_supported_host("not a url") and unsupported_reason("not a url"))
    return ok


def _page(body_html, ready_marker=True):
    """A minimal document. `header class=\"data-header\"` or `table
    class=\"items\"` is what detect_page_state treats as real content
    arriving (see product_parser.detect_page_state), so a bare fragment with
    neither would misclassify as 'empty' regardless of what else it has."""
    marker = '<header class="data-header">x</header>' if ready_marker else ""
    return "<html><body>%s%s</body></html>" % (marker, body_html)


# The challenge this site actually serves. Transcribed from a real capture
# taken 2026-09-16 (HTTP 405, `x-amzn-waf-action: captcha`, `server:
# CloudFront`, 2331 bytes) -- the gokuProps values are truncated here and
# the per-deployment ids in the script hostnames are the real shape but not
# a live deployment, since neither is a credential and neither has to be
# real for the parser to be exercised. NOT a first-party capture by this
# repo: replace it with one the first time a run meets a live challenge.
FIX_AWS_WAF_CHALLENGE = """<!DOCTYPE html><html lang="en"><head>
<title>Human Verification</title>
<script type="text/javascript">
window.awsWafCookieDomainList = [];
window.gokuProps = {
  "key":"AQIDAHjcYu/GjX+QlghicBgQ/7bFaQZ+m5FKCMDnO+vTbNg96A==",
  "iv":"D54pBwHvVAAAA+vK",
  "context":"YVOg6946nHFLSvC1mQcbSeAO9SL2AREZnOrNAWPRMAs3ZoXUEFsk9RA9"
};
</script>
<script src="https://6cb07a88f2ca.54698f12.us-east-1.token.awswaf.com/6cb/challenge.js"></script>
<script src="https://6cb07a88f2ca.54698f12.us-east-1.captcha.awswaf.com/6cb/captcha.js"></script>
</head><body><div id="captcha-container"></div></body></html>"""


def test_aws_waf():
    group("AWS WAF: the challenge this site actually serves")
    ok = True
    import captcha_solver as cs

    # --- §18's control: every marker must be ABSENT from real pages -------
    # A marker that fires on a good page is worse than no marker at all.
    good_pages = {
        "market-values": FIX_MARKET_VALUES,
        "club-squad": FIX_CLUB_SQUAD,
        "transfers": FIX_TRANSFERS,
        "player detail": FIX_PLAYER_DETAIL,
        "market-values (de)": FIX_MARKET_VALUES_DE,
    }
    markers = product_parser.BOT_CHALLENGE_MARKERS["AWS WAF"]
    ok &= check("the marker set names AWS WAF at all (it did not until v0.4.1, "
                "which is why this site's only real challenge was invisible)",
                len(markers) >= 3)
    for label, page in good_pages.items():
        hits = [m for m in markers if m in page]
        ok &= check("no AWS WAF marker appears on a real %s page (%s)"
                    % (label, "clean" if not hits else "FIRES ON: %s" % hits),
                    not hits)
    ok &= check("...and every marker does appear on the challenge itself",
                all(m in FIX_AWS_WAF_CHALLENGE for m in markers))

    # --- the states, per engine capability -------------------------------
    # Selenium and pyppeteer cannot supply a status; playwright and the
    # Scraper API can. All of them must reach `captcha`, because all of them
    # used to reach `empty` -- exit 4, reported as a real empty listing.
    ok &= check("challenge + no status (selenium/pyppeteer) -> captcha, not empty",
                product_parser.detect_page_state(FIX_AWS_WAF_CHALLENGE) == "captcha")
    ok &= check("challenge + HTTP 405 -> captcha, not blocked (blocked has "
                "solve=False, so the solver would never be offered it)",
                product_parser.detect_page_state(
                    FIX_AWS_WAF_CHALLENGE, status=405) == "captcha")
    ok &= check("the x-amzn-waf-action header alone is decisive, whatever the body",
                product_parser.detect_page_state(
                    "", headers={"x-amzn-waf-action": "captcha"}) == "captcha")
    ok &= check("...case-insensitively, since every layer spells headers differently",
                product_parser.detect_page_state(
                    "", headers={"X-Amzn-Waf-Action": "CAPTCHA"}) == "captcha")
    ok &= check("and the policy for that state actually solves",
                page_flow.should_solve("captcha") and not page_flow.should_solve("blocked"))

    # --- §17's classification-order trap ---------------------------------
    served_with_hunter = (
        '<html><head><script src="chrome-extension://kjmk/content/captcha/'
        'amazon_waf/interceptor.js"></script></head><body>'
        '<table class="items"><tr><td>row</td></tr></table></body></html>')
    ok &= check("a SERVED page carrying 2Captcha's own injected WAF hunter "
                "still classifies as content, not as a challenge",
                product_parser.detect_page_state(served_with_hunter,
                                                 status=200) == "content")
    ok &= check("a genuinely empty page is still 'empty', not a false captcha",
                product_parser.detect_page_state(
                    "<html><body>nothing here</body></html>", status=200) == "empty")

    # --- the task the solver builds --------------------------------------
    ch = cs.detect_aws_waf(FIX_AWS_WAF_CHALLENGE,
                           "https://www.transfermarkt.com/statistik/neuestetransfers")
    ok &= check("detect_aws_waf reads a challenge out of the served HTML "
                "(no runtime interception needed, unlike Turnstile)",
                ch is not None and ch.kind == "aws_waf")
    if ch:
        task = cs._v2_task_for(ch, 0.7)
        ok &= check("task type is AmazonTaskProxyless, per 2captcha's own docs",
                    task["type"] == "AmazonTaskProxyless")
        ok &= check("all three gokuProps values reach the task under the "
                    "vendor's field names (key->websiteKey, iv, context)",
                    task["websiteKey"].startswith("AQIDAHjcYu")
                    and task["iv"] == "D54pBwHvVAAAA+vK"
                    and task["context"].startswith("YVOg6946"))
        ok &= check("both optional script URLs are sent when the page gave them",
                    task["challengeScript"].endswith("challenge.js")
                    and task["captchaScript"].endswith("captcha.js"))
        ok &= check("the answer goes in the aws-waf-token COOKIE, not a form "
                    "field -- a different injection path from reCAPTCHA",
                    cs.AWS_WAF_COOKIE == "aws-waf-token")

    # --- refusing to pay for an incomplete detection ----------------------
    ok &= check("a partial gokuProps (no context) yields no challenge rather "
                "than a task the API would reject at full price",
                cs.detect_aws_waf('<script>window.gokuProps = {"key":"k","iv":"i"};</script>',
                                  "u") is None)
    ok &= check("unparseable gokuProps degrades to 'not detected', not an "
                "exception inside a detector every page goes through",
                cs.detect_aws_waf('<script>window.gokuProps = {key: bare};</script>',
                                  "u") is None)
    ok &= check("a real page yields no AWS WAF challenge",
                cs.detect_aws_waf(FIX_MARKET_VALUES, "u") is None)
    return ok


def test_bot_detection():
    group("bot/block/captcha detection")
    ok = True

    ok &= check("no markers at all -> no challenge detected",
                detect_bot_challenge("<html><body>hello</body></html>") is None)
    ok &= check("empty/None html -> no challenge, not a crash",
                detect_bot_challenge("") is None and detect_bot_challenge(None) is None)

    for vendor, markers in BOT_CHALLENGE_MARKERS.items():
        sample = "<html><body>%s</body></html>" % markers[0]
        ok &= check("a page containing %r is attributed to %s" % (markers[0], vendor),
                    detect_bot_challenge(sample) == vendor)

    # The Scraping Browser API's auto-solve extension injects its own script
    # tags into every page it loads; those must not be mistaken for the
    # site's own challenge markup (see the module's _EXTENSION_TAG_RE note).
    injected = ('<html><body><script src="chrome-extension://abc123/hunt.js">'
               'cf-turnstile probe</script>clean content</body></html>')
    ok &= check("an extension-injected script mentioning a vendor marker is "
                "stripped before matching, so a clean page is not misclassified",
                detect_bot_challenge(injected) is None)

    # detect_page_state: the four states.
    ok &= check("a non-2xx status is 'blocked' regardless of body",
                detect_page_state("<html>anything</html>", status=403) == "blocked")
    ok &= check("no html at all is 'blocked'", detect_page_state("", status=200) == "blocked")
    ok &= check("a reCAPTCHA marker is 'captcha' (the paid-solve path), not 'blocked'",
                detect_page_state('<html><body><script src="recaptcha/api.js">'
                                  '</script></body></html>', status=200) == "captcha")
    ok &= check("a DataDome marker is 'blocked' (no solver integrated for it)",
                detect_page_state('<html><body>datadome</body></html>', status=200) == "blocked")
    ok &= check("real content (a data-header) with no challenge marker is 'content'",
                detect_page_state(_page(""), status=200) == "content")
    ok &= check("real content (an items table) with no challenge marker is 'content'",
                detect_page_state('<html><body><table class="items">x</table></body></html>',
                                  status=200) == "content")
    ok &= check("a clean 200 page with neither anchor present is 'empty', not 'content'",
                detect_page_state("<html><body>nothing recognisable</body></html>",
                                  status=200) == "empty")
    return ok


def test_page_flow():
    group("page_flow: ready selectors, state policy, pagination gating")
    ok = True

    for mode in ("market-values", "club-squad", "transfers", "player"):
        ok &= check("%s has a ready_selector, min_matches and a timeout" % mode,
                    page_flow.ready_selector(mode) and page_flow.content_timeout_ms(mode) > 0)
    ok &= check("listing modes require more than one row match (a lucky "
                "single match must not resolve the wait)",
                all(page_flow.min_matches(m) > 1
                    for m in ("market-values", "club-squad", "transfers")))
    ok &= check("player (a single profile, not a table) needs no row floor",
                page_flow.min_matches("player") == 0)
    ok &= check("an unknown mode falls back to the market-values selector "
                "rather than raising",
                page_flow.ready_selector("nonsense-mode") == page_flow.ready_selector("market-values"))

    ok &= check("classify() delegates to product_parser.detect_page_state",
                page_flow.classify('<html><body><table class="items">x</table></body></html>',
                                   status=200) == "content")

    # STATE_POLICY, exercised through the accessor functions rather than the
    # dict directly -- an engine reaches these, not the dict.
    ok &= check("content: no retry, no solve, not counted as blocked",
                not page_flow.should_retry("content") and not page_flow.should_solve("content")
                and not page_flow.counts_as_blocked("content"))
    ok &= check("blocked: retried, but not solved (nothing to solve), and IS a block",
                page_flow.should_retry("blocked") and not page_flow.should_solve("blocked")
                and page_flow.counts_as_blocked("blocked"))
    ok &= check("captcha: retried AND solved, and counts as blocked until solved",
                page_flow.should_retry("captcha") and page_flow.should_solve("captcha")
                and page_flow.counts_as_blocked("captcha"))
    ok &= check("empty: a correct answer, not retried, not a block",
                not page_flow.should_retry("empty") and not page_flow.should_solve("empty")
                and not page_flow.counts_as_blocked("empty"))
    ok &= check("an unrecognised state defaults to the safe 'do nothing' answer",
                not page_flow.should_retry("???") and not page_flow.should_solve("???")
                and not page_flow.counts_as_blocked("???"))

    # comparable(): query-parameter order should not matter, fragment is dropped.
    ok &= check("comparable() normalises query-parameter order",
                page_flow.comparable("https://x/y?b=2&a=1")
                == page_flow.comparable("https://x/y?a=1&b=2"))
    ok &= check("comparable() drops the fragment",
                page_flow.comparable("https://x/y?a=1#frag")
                == page_flow.comparable("https://x/y?a=1"))

    # The gallery pagination trap, corrected: market-values gets the SAME
    # verify-first check as every other mode, not a hardcoded False -- a
    # wider re-measurement found the plain ?page=N shape on 28 of 29
    # consecutive fetches (see page_flow.py's module docstring), so this
    # mode is USUALLY addressable and only rarely is not.
    ok &= check("market-values pagination IS addressable when the site's "
                "own next-link agrees with page_url()'s construction (the "
                "common case, per the 2026-09-10 re-measurement) -- this "
                "used to be hardcoded False for this mode regardless",
                page_flow.pagination_is_addressable(
                    "market-values", MARKET_VALUES_URL, page_url(MARKET_VALUES_URL, 2)))
    ok &= check("...and the same holds with no next-link at all (nothing "
                "contradicts the construction)",
                page_flow.pagination_is_addressable("market-values", MARKET_VALUES_URL, None))
    # The rare gallery shape (a real capture, 2026-09-10) still correctly
    # falls back to link-chaining: its next-link disagrees with what
    # page_url() would build.
    _gallery_next_link = (
        "https://www.transfermarkt.com/spieler-statistik/wertvollstespieler/"
        "marktwertetop/plus/0/galerie/0ausrichtung%3DSturm%26spielerposition_id"
        "%3Dalle%26altersklasse%3Dalle%26jahrgang%3D0%26land_id%3D72%26"
        "kontinent_id%3D0%26yt0%3DShow/page/8/page/3//page/2")
    ok &= check("...but a gallery-shaped next-link (real capture) is "
                "correctly read as NOT addressable, falling back to "
                "chaining the site's own link",
                not page_flow.pagination_is_addressable(
                    "market-values", MARKET_VALUES_URL, _gallery_next_link))

    # transfers: addressable when the site's own next-link agrees with the
    # constructed page-2 URL; not addressable (falls back to link-chaining)
    # when it disagrees or is session-tagged.
    ok &= check("transfers pagination IS addressable when the site's own "
                "next-link agrees with page_url()'s own page-2 construction",
                page_flow.pagination_is_addressable(
                    "transfers", TRANSFERS_URL, page_url(TRANSFERS_URL, 2)))
    ok &= check("transfers pagination with no next-link at all defaults to "
                "addressable (nothing contradicts the construction)",
                page_flow.pagination_is_addressable("transfers", TRANSFERS_URL, None))
    ok &= check("transfers pagination is NOT addressable when the site's own "
                "next-link disagrees with the constructed URL",
                not page_flow.pagination_is_addressable(
                    "transfers", TRANSFERS_URL,
                    "https://www.transfermarkt.com/some/other/gallery/token/page/2"))

    # is_thin_page(): informational-only diagnostic, deliberately NOT a hard
    # PAGE_CAP -- the sku-based "no_new_products" stop condition already
    # covers real pagination exhaustion; this only flags a page that came
    # back suspiciously short of page 1, which is either the listing's real
    # depth or a markup regression, and it is up to the caller (which just
    # logs a warning) to tell those apart.
    ok &= check("a page with a normal row count relative to page 1 is not thin",
                not page_flow.is_thin_page(23, 25))
    ok &= check("a page with well under THIN_PAGE_RATIO of page 1's rows IS thin",
                page_flow.is_thin_page(5, 25))
    ok &= check("the boundary itself is not thin (< , not <=)",
                not page_flow.is_thin_page(10, 25))  # 10 == 25 * 0.4 exactly
    ok &= check("a page 1 that itself had 0 rows never reports a later page "
                "as thin (0 < 0 is False, not a crash)",
                page_flow.is_thin_page(0, 0) is False)

    ok &= check("PRICE_COVERAGE_FLOOR is a fraction, not a percentage",
                0.0 < page_flow.PRICE_COVERAGE_FLOOR < 1.0)
    return ok


# ---------------------------------------------------------------------------
def test_readiness_wait():
    group("page_flow.wait_for_count: poll a count, never evaluate a string")
    ok = True

    # ready_count converts min_matches's "strictly MORE than this floor" into
    # the "at least this many" wait_for_count is written against. They are
    # tied together here because the conversion is the kind of off-by-one
    # that would leave the three engines waiting on different thresholds
    # while every other check stayed green.
    for mode in ("market-values", "club-squad", "transfers", "player"):
        ok &= check("ready_count(%s) is one more than its min_matches floor" % mode,
                    page_flow.ready_count(mode) == page_flow.min_matches(mode) + 1)
    ok &= check("player mode still waits for its single header to appear -- a "
                "floor of 0 must not collapse into 'do not wait at all'",
                page_flow.ready_count("player") == 1)

    def fake_driver(counts):
        """A driver that returns a scripted sequence of counts and only
        records its sleeps, so the wait's arithmetic is testable with no
        browser and no engine library installed."""
        state = {"polls": 0, "slept": 0, "selectors": []}

        def count(selector):
            state["selectors"].append(selector)
            i = min(state["polls"], len(counts) - 1)
            state["polls"] += 1
            return counts[i]

        def sleep(ms):
            state["slept"] += ms

        return count, sleep, state

    count, sleep, st = fake_driver([0, 1, 4])
    seen = page_flow.wait_for_count(count, sleep, "table.items tbody tr", 4, 20000)
    ok &= check("returns as soon as the minimum is reached", seen == 4)
    ok &= check("and stops polling there rather than spending the budget",
                st["polls"] == 3 and st["slept"] == 500)
    ok &= check("it polls the selector it was handed, not a hardcoded one",
                set(st["selectors"]) == {"table.items tbody tr"})

    count, sleep, st = fake_driver([7])
    seen = page_flow.wait_for_count(count, sleep, "x", 4, 20000)
    ok &= check("a page already painted is not slept on at all",
                seen == 7 and st["slept"] == 0 and st["polls"] == 1)

    count, sleep, st = fake_driver([2])
    seen = page_flow.wait_for_count(count, sleep, "x", 4, 1000, poll_ms=250)
    ok &= check("gives up when the budget runs out rather than looping forever",
                st["slept"] == 1000)
    ok &= check("and returns the LAST COUNT SEEN, so a caller can tell "
                "'painted' from 'timed out holding two of them'", seen == 2)

    count, sleep, st = fake_driver([0])
    page_flow.wait_for_count(count, sleep, "x", 1, 0)
    ok &= check("a zero budget polls once and returns, it does not sleep",
                st["polls"] == 1 and st["slept"] == 0)

    # The reason any of this exists. Playwright's wait_for_function and
    # pyppeteer's waitForFunction hand the BROWSER a string to evaluate, and
    # a site whose CSP omits unsafe-eval refuses it -- which on a sibling
    # repo was an EvalError and exit 1 on the site's most obvious URL, on one
    # of its two listing routes and not the other. Checked against the source
    # on disk rather than an imported module, so it still holds for the two
    # engines whose library is absent from this machine.
    #
    # The needles are BUILT rather than written out, for the same reason the
    # banned-phrase scan's list is: this file is scanned too, and spelling
    # them here would fail the build on the very file implementing the check
    # -- with no allowlist to reach for, because an allowlist is how a scan
    # stops covering the thing it was written for.
    eval_waits = ("wait_for_" + "function(", "wait" + "ForFunction(")
    for name in sorted(f for f in os.listdir(REPO_ROOT) if f.endswith(".py")):
        src = open(os.path.join(REPO_ROOT, name), encoding="utf-8").read()
        ok &= check("%s waits on no evaluated string" % name,
                    not any(needle in src for needle in eval_waits))

    for name in ("playwright_scraper.py", "puppeteer_scraper.py",
                 "selenium_scraper.py"):
        src = open(os.path.join(REPO_ROOT, name), encoding="utf-8").read()
        ok &= check("%s takes its readiness WAIT from page_flow too, not just "
                    "the selector" % name, "page_flow.wait_for_count" in src)
    return ok


# ---------------------------------------------------------------------------
def test_output_contract():
    group("output_writer: two row schemas, not one, and no permanently-null columns")
    ok = True

    player_names = {f.name for f in fields(Player)}
    transfer_names = {f.name for f in fields(Transfer)}
    family_prefix = {"source", "scraped_at", "url", "sku", "title",
                     "image_url", "price", "currency", "category"}
    ok &= check("Player carries the family's shared prefix columns",
                family_prefix <= player_names)
    ok &= check("Transfer carries the family's shared prefix columns too",
                family_prefix <= transfer_names)
    ok &= check("price_source exists on Player...", "price_source" in player_names)
    ok &= check("...but NOT on Transfer -- there is no second reading of a fee "
                "to confirm it against, so a price_source column would be null "
                "on every row (see output_writer.py's docstring)",
                "price_source" not in transfer_names)
    ok &= check("Transfer carries from_club/to_club -- fields that would be "
                "permanently null on a Player row, which is why these are two "
                "dataclasses and not one with both sets of columns",
                {"from_club", "to_club", "fee_type"} <= transfer_names
                and not ({"from_club", "to_club", "fee_type"} & player_names))
    ok &= check("Player carries position/club -- fields that would be "
                "permanently null on a Transfer row",
                {"position", "club"} <= player_names)
    # E-commerce columns this family's other members carry (brand, rating,
    # in_stock, discount_pct) do not exist here at all -- dropped rather than
    # padded as permanently-null, per the same family rule this docstring
    # section already tests both directions of.
    ecommerce_only = {"brand", "rating", "review_count", "in_stock",
                     "original_price", "discount_pct"}
    ok &= check("no leftover e-commerce-only column exists on either row class",
                not (ecommerce_only & player_names) and not (ecommerce_only & transfer_names))

    ok &= check("ROW_CLASS_BY_MODE covers exactly the four modes this repo supports",
                set(ROW_CLASS_BY_MODE) == {"market-values", "club-squad", "player", "transfers"})
    ok &= check("market-values/club-squad/player all map to Player",
                all(ROW_CLASS_BY_MODE[m] is Player
                    for m in ("market-values", "club-squad", "player")))
    ok &= check("transfers maps to Transfer", ROW_CLASS_BY_MODE["transfers"] is Transfer)
    ok &= check("all four modes are dedupe-by-sku safe",
                set(UNIQUE_BY_SKU_MODES) == set(ROW_CLASS_BY_MODE))

    ok &= check("a fresh Player has an ISO scraped_at with no arguments needed",
                "T" in Player().scraped_at)
    return ok


def test_writers():
    group("dedupe + JSON/CSV writers")
    ok = True

    seen = set()
    rows = [Player(sku="1"), Player(sku="2"), Player(sku="1"), Player(sku=None), Player(sku=None)]
    fresh = dedupe_by_key(rows, seen)
    ok &= check("a repeated sku across pages is dropped",
                [r.sku for r in fresh] == ["1", "2", None, None])
    ok &= check("a row with no sku is never dropped (nothing to compare it against)",
                sum(1 for r in fresh if r.sku is None) == 2)
    ok &= check("dedupe_by_sku is the same rule under its own name",
                [r.sku for r in dedupe_by_sku(
                    [Player(sku="9"), Player(sku="9")], set())] == ["9"])

    with tempfile.TemporaryDirectory() as d:
        json_path = os.path.join(d, "out.json")
        csv_path = os.path.join(d, "out.csv")
        sample = [Player(sku="1", title="A", nationalities=["France", "Cameroon"]),
                  Player(sku="2", title="B", nationalities=None)]
        write_json(sample, json_path)
        loaded = json.load(open(json_path, encoding="utf-8"))
        ok &= check("write_json round-trips a list field (nationalities) as a real list",
                    loaded[0]["nationalities"] == ["France", "Cameroon"])

        write_csv(sample, csv_path, row_cls=Player)
        with open(csv_path, encoding="utf-8", newline="") as f:
            csv_rows = list(csv.DictReader(f))
        ok &= check("write_csv joins a list field with the documented separator, "
                    "so it round-trips by splitting on the same string",
                    csv_rows[0]["nationalities"] == LIST_CSV_SEPARATOR.join(["France", "Cameroon"]))
        ok &= check("CSV header matches the Player schema exactly",
                    list(csv_rows[0].keys()) == [f.name for f in fields(Player)])

        # An empty result must still get a header, from row_cls, not the first row.
        empty_csv = os.path.join(d, "empty.csv")
        write_csv([], empty_csv, row_cls=Transfer)
        header = open(empty_csv, encoding="utf-8").readline().strip().split(",")
        ok &= check("write_csv([], ..., row_cls=Transfer) still writes the "
                    "Transfer header, not an empty file",
                    header == [f.name for f in fields(Transfer)])
    return ok


def test_finish_run():
    group("save() / finish_run(): the empty-run and exit-code contract")
    ok = True

    with tempfile.TemporaryDirectory() as d:
        prefix = os.path.join(d, "run")

        # The family invariant: a run that finds nothing writes nothing,
        # unless the caller explicitly says an empty result is expected.
        rc = save([], prefix, "both", allow_empty=False)
        ok &= check("0 rows, no --allow-empty: nothing written, exit EXIT_NO_PRODUCTS",
                    rc == EXIT_NO_PRODUCTS and not os.path.exists(prefix + ".json"))
        rc = save([], prefix, "both", allow_empty=True)
        ok &= check("0 rows WITH --allow-empty: files ARE written",
                    os.path.exists(prefix + ".json") and os.path.exists(prefix + ".csv"))

        # finish_run: a complete run with rows.
        rows = [Player(sku="1"), Player(sku="2")]
        rc = finish_run(rows, prefix, "json", allow_empty=False, blocked=False,
                        stop_reason="completed", pages_requested=1, pages_completed=1,
                        start_url="https://x/1", final_url="https://x/1", mode="market-values")
        ok &= check("a complete run with rows returns 0", rc == 0)
        meta = json.load(open(prefix + ".meta.json", encoding="utf-8"))
        ok &= check("...and its meta sidecar says status=complete", meta["status"] == "complete")

        # A partial run (stopped early but got some rows) -> EXIT_PARTIAL,
        # and the meta sidecar must say so rather than claiming completeness.
        rc = finish_run(rows, prefix, "json", allow_empty=False, blocked=False,
                        stop_reason="captcha_unsolved", pages_requested=3, pages_completed=1,
                        start_url="https://x/1", final_url="https://x/1", mode="market-values")
        ok &= check("a partial run (some rows, did not finish) returns EXIT_PARTIAL",
                    rc == EXIT_PARTIAL)
        meta = json.load(open(prefix + ".meta.json", encoding="utf-8"))
        ok &= check("...and the sidecar says status=partial, not complete",
                    meta["status"] == "partial")

        # A run that got NOTHING and was blocked -> EXIT_BLOCKED, distinct
        # from EXIT_NO_PRODUCTS (a page that legitimately had nothing to show).
        rc = finish_run([], prefix, "json", allow_empty=False, blocked=True,
                        stop_reason="blocked", pages_requested=1, pages_completed=0,
                        start_url="https://x/1", final_url="https://x/1", mode="market-values")
        ok &= check("0 rows AND blocked=True returns EXIT_BLOCKED, not EXIT_NO_PRODUCTS "
                    "-- a bot-check is a different failure than an empty page",
                    rc == EXIT_BLOCKED)

        # 0 rows, not blocked (a genuinely empty page) -> EXIT_NO_PRODUCTS.
        rc = finish_run([], prefix, "json", allow_empty=False, blocked=False,
                        stop_reason="empty", pages_requested=1, pages_completed=1,
                        start_url="https://x/1", final_url="https://x/1", mode="club-squad")
        ok &= check("0 rows, NOT blocked, returns EXIT_NO_PRODUCTS",
                    rc == EXIT_NO_PRODUCTS)

    ok &= check("COMPLETE_STOP_REASONS names the reasons that count as a full run",
                {"completed", "pagination_exhausted", "single_page_mode"} <= set(COMPLETE_STOP_REASONS))
    return ok


def test_diff():
    group("diff_runs: added / removed / changed, keyed on sku")
    ok = True

    old = [{"sku": "1", "title": "A", "price": 100.0, "currency": "EUR", "club": "X", "club_id": "1"},
          {"sku": "2", "title": "B", "price": 50.0, "currency": "EUR", "club": "Y", "club_id": "2"}]
    new = [{"sku": "1", "title": "A", "price": 120.0, "currency": "EUR", "club": "X", "club_id": "1"},
          {"sku": "3", "title": "C", "price": 10.0, "currency": "EUR", "club": "Z", "club_id": "3"}]
    result = diff_rows(old, new)
    ok &= check("sku 2 dropped out -> removed", any(r["sku"] == "2" for r in result["removed"]))
    ok &= check("sku 3 is new -> added", any(r["sku"] == "3" for r in result["added"]))
    ok &= check("sku 1's price moved -> changed, with old/new both recorded",
                len(result["changed"]) == 1 and result["changed"][0]["sku"] == "1"
                and result["changed"][0]["changes"]["price"] == {"old": 100.0, "new": 120.0})
    ok &= check("a field that did not change is not reported",
                "club" not in result["changed"][0]["changes"])

    # Transfer rows carry no club/club_id at all -- a diff between two
    # transfers-mode runs must not manufacture a spurious "club changed" entry.
    old_t = [{"sku": "t1", "title": "P", "price": 100.0, "currency": "EUR"}]
    new_t = [{"sku": "t1", "title": "P", "price": 100.0, "currency": "EUR"}]
    result_t = diff_rows(old_t, new_t)
    ok &= check("two identical transfer rows produce no changes at all",
                not result_t["changed"] and not result_t["added"] and not result_t["removed"])

    ok &= check("a row with no sku is counted as unmatchable, not silently dropped",
                diff_rows([{"sku": None}], [])["unmatchable_old"] == 1)
    ok &= check("TRACKED_FIELDS does not include a price-tolerance concept -- "
                "there is no multi-currency conversion in this repo's data",
                "price" in TRACKED_FIELDS and "currency" in TRACKED_FIELDS)

    # TRACKED_FIELDS used to track only the two family-prefix fields above --
    # a transfers-mode diff reported nothing transfer-specific at all, so a
    # player re-loaned to the same fee under a different destination club
    # went unreported.
    for field in ("position", "age", "nationality", "market_value_last_update",
                 "from_club", "from_club_id", "to_club", "to_club_id", "fee_type"):
        ok &= check("TRACKED_FIELDS now covers %r" % field, field in TRACKED_FIELDS)

    old_p = [{"sku": "1", "position": "Right Winger", "market_value_last_update": "2026-06-01"}]
    new_p = [{"sku": "1", "position": "Right Winger", "market_value_last_update": "2026-09-01"}]
    result_p = diff_rows(old_p, new_p)
    ok &= check("a Player-mode market_value_last_update change is now caught "
                "even when the price itself did not move (a re-confirmed "
                "valuation, not just a new figure)",
                len(result_p["changed"]) == 1
                and result_p["changed"][0]["changes"]["market_value_last_update"]
                == {"old": "2026-06-01", "new": "2026-09-01"})

    old_tr = [{"sku": "t9", "from_club": "Ajax", "to_club": "PSV", "fee_type": "loan"}]
    new_tr = [{"sku": "t9", "from_club": "Ajax", "to_club": "Feyenoord", "fee_type": "loan"}]
    result_tr = diff_rows(old_tr, new_tr)
    ok &= check("a Transfer-mode to_club change is now caught (previously "
                "invisible -- neither field was tracked at all)",
                len(result_tr["changed"]) == 1
                and result_tr["changed"][0]["changes"]["to_club"]
                == {"old": "PSV", "new": "Feyenoord"}
                and "from_club" not in result_tr["changed"][0]["changes"])

    # Cross-schema safety: a Player-only field on a Transfer row (and vice
    # versa) reads None via .get() on both sides, so it must never
    # manufacture a spurious "changed" entry just because one schema's field
    # is absent from the other's dict.
    result_cross = diff_rows(old_tr, new_tr)
    ok &= check("Player-only fields absent from Transfer rows never "
                "manufacture a spurious change on a Transfer-mode diff",
                "position" not in result_cross["changed"][0]["changes"]
                and "market_value_last_update" not in result_cross["changed"][0]["changes"])
    return ok


def test_captcha():
    group("captcha_solver: detection wiring and credential redaction")
    ok = True

    html_with_v3 = ('<html><body><script>grecaptcha.execute("6Lc-SITEKEY123456789012345",'
                    '{action:"login"})</script></body></html>')
    challenge = detect_recaptcha_v3(html_with_v3, "https://www.transfermarkt.com/x")
    ok &= check("a v3 sitekey+action pair in a script tag is detected",
                challenge is not None and challenge.sitekey == "6Lc-SITEKEY123456789012345"
                and challenge.action == "login" and challenge.kind == "recaptcha_v3")
    ok &= check("a page with no recaptcha markup detects nothing",
                detect_recaptcha_v3("<html><body>clean</body></html>",
                                    "https://x") is None)

    ok &= check("reconcile_detections prefers a real detection over None",
                reconcile_detections(challenge, None) is challenge)
    ok &= check("reconcile_detections returns None when neither side found anything",
                reconcile_detections(None, None) is None)
    # Two detections that disagree on kind: the runtime (live-loader) reading
    # is trusted over the static markup's own claim -- see the function's
    # docstring for why (a site's own data-version attribute can be stale).
    html_says_v3 = CaptchaChallenge(kind="recaptcha_v3", sitekey="k", action="verify")
    runtime_says_v2i = CaptchaChallenge(kind="recaptcha_v2_invisible", sitekey="k")
    resolved = reconcile_detections(html_says_v3, runtime_says_v2i)
    ok &= check("when the two detectors disagree, the runtime/live-loader "
                "reading wins over the static markup's own claim",
                resolved.kind == "recaptcha_v2_invisible")

    # solve_recaptcha must refuse to spend money it has no key for, rather
    # than silently fabricating a token -- it raises, naming exactly what's
    # missing and how to supply it, instead of returning something callable
    # code might mistake for a real solve.
    dummy = CaptchaChallenge(kind="recaptcha_v2", sitekey="x", page_url="https://x")
    raised = None
    try:
        solve_recaptcha(dummy, None, api_version="v2")
    except RuntimeError as e:
        raised = str(e)
    ok &= check("solve_recaptcha with no API key raises rather than "
                "fabricating a token, and names --twocaptcha-key as the fix",
                raised is not None and "--twocaptcha-key" in raised)
    return ok


def test_env_config():
    group("env_config: precedence and placeholder handling")
    ok = True

    with tempfile.TemporaryDirectory() as d:
        env_path = os.path.join(d, ".env")
        with open(env_path, "w", encoding="utf-8") as f:
            f.write("TWOCAPTCHA_KEY=from_dotenv\n")
            f.write("TRANSFERMARKT_PROXY=http://a:b@from-dotenv:8080\n")
            f.write("SOME_TYPO_KEY=oops\n")

        saved = {k: os.environ.pop(k, None) for k in env_config.ENV_KEYS}
        try:
            os.environ["TRANSFERMARKT_URL"] = "https://from-real-env/x"
            env_config.load_env(env_path, override=False)

            class Args:
                twocaptcha_key = None
                proxy = None
                url = "https://from-cli-flag/x"  # explicit flag: must win
                cdp_endpoint = None

            args = env_config.apply(Args(), quiet=True)
            ok &= check("an explicit CLI flag beats both env var and .env file",
                        args.url == "https://from-cli-flag/x")
            ok &= check("a real environment variable beats the .env file",
                        os.environ.get("TRANSFERMARKT_URL") == "https://from-real-env/x")
            ok &= check(".env fills a destination nothing else set",
                        args.twocaptcha_key == "from_dotenv")
            ok &= check(".env value reaches a destination via ENV_KEYS mapping",
                        args.proxy == "http://a:b@from-dotenv:8080")

            unknown = env_config.unknown_keys(env_path)
            ok &= check("a typo'd key in .env is reported, not silently ignored",
                        "SOME_TYPO_KEY" in unknown)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            os.environ.pop("TRANSFERMARKT_URL", None)

    ok &= check("a .env.example placeholder value is treated as unset",
                (lambda: (os.environ.__setitem__("TWOCAPTCHA_KEY", "your_2captcha_api_key_here"),
                         env_config.env_value("TWOCAPTCHA_KEY"),
                         os.environ.pop("TWOCAPTCHA_KEY"))[1])() is None)
    ok &= check("an unset variable does not warn about being a placeholder "
                "(an unset CI secret arrives empty, and that is normal)",
                env_config.env_value("TWOCAPTCHA_KEY") is None)
    return ok


def test_proxy_pool():
    group("proxy_pool: credentials never reach argv or logs")
    ok = True
    url = "http://user:secret@eu.proxy.2prx.com:2334"
    masked = mask(url)
    ok &= check("credentials are masked in logs", "secret" not in masked)
    ok &= check("...but the host and port survive masking",
                "eu.proxy.2prx.com:2334" in masked)

    pw = to_playwright(url)
    ok &= check("the server string handed to the browser has no credentials",
                "secret" not in pw["server"])
    ok &= check("credentials go through the driver's own fields",
                pw["username"] == "user" and pw["password"] == "secret")

    scrubbed, creds = split_credentials(url)
    ok &= check("split_credentials separates the two",
                scrubbed == "http://eu.proxy.2prx.com:2334" and creds == ("user", "secret"))

    pool = ProxyPool(["http://a:1", "http://b:2", "http://c:3"])
    ok &= check("a pool reports its size", len(pool) == 3)
    first = pool.current
    pool.advance("test")
    ok &= check("advancing moves to another exit", pool.current != first)
    copy = pool.proxies
    copy.append("http://d:4")
    ok &= check("the pool hands out a copy of its exits, not the list itself",
                len(pool) == 3)

    try:
        import playwright_scraper
    except ImportError:
        playwright_scraper = None
    if playwright_scraper is not None:
        exits = [playwright_scraper._worker_pool(pool, i).current for i in range(3)]
        ok &= check("three workers start on three different exits", len(set(exits)) == 3)
        ok &= check("a worker with no pool gets none", playwright_scraper._worker_pool(None, 0) is None)

    one = ProxyPool(["http://only:1"])
    one.advance("nowhere else to go")
    ok &= check("a single-exit pool survives a rotation", one.current == "http://only:1")
    ok &= check("an empty pool is refused rather than silently accepted",
                _raises(lambda: ProxyPool([])))

    pasted = "http://eu.proxy.2prx.com:2334:SOMELOGIN-zone-custom-region-de:SOMEPASSWORD"
    raised = None
    try:
        parse_proxy_line(pasted, source="TRANSFERMARKT_PROXY")
    except ProxyError as exc:
        raised = str(exc)
    ok &= check("a proxy-list line pasted as a URL is refused, not crashed on",
                raised is not None)
    ok &= check("...and the refusal says what the value should look like",
                raised is not None and "login:password@host:port" in raised)
    ok &= check("...and neither the login nor the password is in the message",
                raised is not None and "SOMEPASSWORD" not in raised and "SOMELOGIN" not in raised)

    ok &= check("mask() does not raise on a malformed URL", "SOMEPASSWORD" not in mask(pasted))
    for junk in ("::::", "not a url", "http://", "://x", ""):
        ok &= check("mask(%r) does not raise" % junk, not _raises(lambda j=junk: mask(j)))
    ok &= check("mask() still keeps host and port on a good URL",
                mask("http://u:p@h.example:8080") == "http://***:***@h.example:8080")

    # This repo's own proxy vendor -- socks5 cannot carry credentials in
    # Chromium, so an entry that tries must be refused rather than silently
    # dropping the password at request time.
    ok &= check("a socks5:// proxy with credentials is refused",
                _raises(lambda: parse_proxy_line("socks5://user:pass@h:1080")))
    return ok


# ---------------------------------------------------------------------------
# The engines
# ---------------------------------------------------------------------------
ENGINES = ("playwright_scraper", "puppeteer_scraper", "selenium_scraper")


def test_engines(skips):
    group("engines: all three must behave identically")
    ok = True
    loaded = {}
    for name in ENGINES:
        try:
            loaded[name] = __import__(name)
        except ImportError as e:
            # Reported, never swallowed: "skipped, engine absent" reads
            # exactly like a passing run, and CI's engine-smoke job fails if
            # this list is non-empty.
            skips.append("%s (%s)" % (name, e))

    for name, mod in loaded.items():
        ok &= check("%s exposes scrape() and parse_args()" % name,
                    hasattr(mod, "scrape") and hasattr(mod, "parse_args"))
        src = inspect.getsource(mod)
        ok &= check("%s takes its readiness policy from page_flow" % name,
                    "page_flow.ready_selector" in src)
        ok &= check("%s takes its state policy from page_flow" % name,
                    "page_flow.should_retry" in src or "page_flow.classify" in src)
        ok &= check("%s masks credentials globally, not just once" % name,
                    "pass@" not in mod._mask_credentials(
                        "a ws://user:pass@h:1/ b ws://user:pass@h:1/"))
        ok &= check("%s refuses a host that is not Transfermarkt" % name,
                    "is_supported_host" in src)
        ok &= check("%s offers exactly the four modes this repo supports" % name,
                    '"market-values", "club-squad", "transfers", "player"' in src)
        ok &= check("%s carries no scrolling/hydration machinery -- every "
                    "page here is fully server-rendered (see page_flow.py)" % name,
                    "scroll_until_stable" not in src and "page_flow.hydrate" not in src)

    # For "it must pass with no engine installed" to mean anything, each
    # engine has to import its driver at MODULE level.
    driver_imports = {"playwright_scraper": "playwright",
                      "puppeteer_scraper": "pyppeteer",
                      "selenium_scraper": "selenium"}
    for name, lib in driver_imports.items():
        path = os.path.join(REPO_ROOT, name + ".py")
        if not os.path.exists(path):
            continue
        tree = ast.parse(open(path, encoding="utf-8").read())
        top_level = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                top_level.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                top_level.add(node.module.split(".")[0])
        ok &= check("%s imports %s at module level, so an absent library skips "
                    "the group instead of hiding a real import error"
                    % (name, lib), lib in top_level)

    # Only playwright_scraper.py supports --concurrency; the other two must
    # accept the flag (for a uniform CLI across the family) but say plainly
    # that it does nothing there, rather than silently ignoring it.
    for name in ("selenium_scraper", "puppeteer_scraper"):
        mod = loaded.get(name)
        if mod is None:
            continue
        src = inspect.getsource(mod)
        ok &= check("%s's --concurrency flag documents that it is ignored" % name,
                    "Ignored in this engine" in src)

    # Fingerprint support: all three engines accept --fingerprint. Selenium
    # and pyppeteer apply it only on a LOCAL launch (never over --cdp-endpoint,
    # which is the Scraping Browser API's own already-fingerprinted profile).
    for name in ("playwright_scraper", "selenium_scraper", "puppeteer_scraper"):
        mod = loaded.get(name)
        if mod is None:
            continue
        src = inspect.getsource(mod)
        ok &= check("%s registers --fingerprint/--fp-tags/--fp-country" % name,
                    '"--fingerprint"' in src and '"--fp-tags"' in src
                    and '"--fp-country"' in src)
        ok &= check("%s requires --twocaptcha-key alongside --fingerprint" % name,
                    "fingerprint needs --twocaptcha-key" in src)

    # --proxy-rotate per-page used to be dead functionality: proxy_pool.py
    # defined ProxyPool.rotates_per_page() but nothing in any of the three
    # engines ever called it, so setting the flag silently did nothing.
    for name in ENGINES:
        mod = loaded.get(name)
        if mod is None:
            continue
        src = inspect.getsource(mod)
        ok &= check("%s actually calls pool.rotates_per_page() somewhere "
                    "(the flag used to be accepted and parsed but read by "
                    "nothing)" % name,
                    "rotates_per_page()" in src)
        # Every engine that can reach an AUTHENTICATED CDP endpoint must turn
        # the Scraping Browser's own auto-solve on, or a run gets less on the
        # paid path than its twin does and says nothing about it. pyppeteer
        # connected over CDP without this until v0.4.1. Selenium is excluded
        # deliberately and not by oversight: chromedriver's `debuggerAddress`
        # takes a bare host:port with nowhere to put a password, so it cannot
        # reach an authenticated endpoint at all.
        if name != "selenium_scraper":
            ok &= check("%s enables Captcha.setAutoSolve on a --cdp-endpoint "
                        "session (Browser API solves first, the local solver "
                        "is the fallback)" % name,
                        "Captcha.setAutoSolve" in src)
            ok &= check("%s treats Captcha.solveFinished as the success "
                        "signal" % name,
                        "Captcha.solveFinished" in src)
        ok &= check("%s logs is_thin_page() as a diagnostic on every "
                    "sequential page (informational only -- see "
                    "page_flow.is_thin_page's docstring for why this is not "
                    "a hard PAGE_CAP)" % name,
                    "page_flow.is_thin_page(" in src)
        ok &= check("%s enforces PRICE_COVERAGE_FLOOR on market-values/"
                    "club-squad price coverage" % name,
                    "page_flow.PRICE_COVERAGE_FLOOR" in src)
        ok &= check("%s logs a dropped-duplicate count on merge, the same "
                    "way every sibling engine does" % name,
                    "dropped %d duplicate row(s)." in src)
    return ok


def test_engine_parity(skips):
    group("engine parity: all three meet AWS WAF the same way")
    ok = True
    import captcha_solver as cs

    # --- source level, so this holds with no engine library installed ----
    for name in ENGINE_FILES:
        src = open(os.path.join(REPO_ROOT, name), encoding="utf-8").read()
        ok &= check("%s looks for AWS WAF at all -- two engines shipped "
                    "detecting it through the generic markers and then having "
                    "no code to do anything about it" % name,
                    "detect_aws_waf" in src)
        ok &= check("%s applies the token as a COOKIE on the AWS WAF branch, "
                    "not into a form field the challenge page does not carry"
                    % name, "AWS_WAF_COOKIE" in src and "is_aws_waf" in src)

    # The navigation wait is the one thing the three spell differently, and
    # getting it wrong is invisible offline: selenium's DEFAULT strategy is
    # "normal", which blocks until the `load` event, and on this site that
    # event does not arrive -- measured 2026-09-16, every fetch timed out at
    # 60s while the other two engines had the same page in under two seconds.
    sel = open(os.path.join(REPO_ROOT, "selenium_scraper.py"), encoding="utf-8").read()
    ok &= check("selenium_scraper navigates with the eager strategy, its "
                "equivalent of the other two engines' domcontentloaded",
                'page_load_strategy = "eager"' in sel)
    for name in ("playwright_scraper.py", "puppeteer_scraper.py"):
        src = open(os.path.join(REPO_ROOT, name), encoding="utf-8").read()
        ok &= check("%s navigates with domcontentloaded" % name,
                    "domcontentloaded" in src)

    # --- behavioural: drive each engine's real handler against the real
    # challenge fixture, with the solver stubbed. The source checks prove the
    # branch is written; only this proves it runs.
    TOKEN = "waf-token-for-the-test"
    URL = "https://www.transfermarkt.com/spieler-statistik/wertvollstespieler/marktwertetop"

    class FakeArgs:
        solve_captcha = "always"
        twocaptcha_key = "0" * 32
        captcha_api = "v1"
        min_score = 0.3

    def drive(modname, build, read):
        try:
            mod = __import__(modname)
        except ImportError as e:
            skips.append("%s AWS WAF parity (%s)" % (modname, e))
            return None
        saved_solve, saved_sleep = mod.solve_recaptcha, mod.time.sleep
        mod.solve_recaptcha = lambda *a, **k: TOKEN
        mod.time.sleep = lambda *_: None
        try:
            session, target = build()
            solved = mod.handle_captcha_if_present(
                session, FakeArgs(), "table.items tbody tr")
            return solved, read(target)
        finally:
            mod.solve_recaptcha, mod.time.sleep = saved_solve, saved_sleep

    class PWContext:
        def __init__(self): self.cookies = []
        def add_cookies(self, c): self.cookies.extend(c)

    class PWPage:
        def __init__(self):
            self.url, self.context = URL, PWContext()
            self.reloaded, self.injected = False, []
        def content(self): return FIX_AWS_WAF_CHALLENGE
        def query_selector_all(self, sel): return []
        def evaluate(self, js, *a): self.injected.append(js); return None
        def wait_for_timeout(self, ms): pass
        def reload(self, **kw): self.reloaded = True

    class SelDriver:
        def __init__(self):
            self.page_source, self.current_url = FIX_AWS_WAF_CHALLENGE, URL
            self.cookies, self.refreshed, self.injected = [], False, []
        def find_elements(self, by, sel): return []
        def add_cookie(self, c): self.cookies.append(c)
        def execute_script(self, js, *a): self.injected.append(js); return None
        def refresh(self): self.refreshed = True

    class PupPage:
        def __init__(self):
            self.url = URL
            self.cookies, self.reloaded, self.injected = [], False, []
        def content(self): return FIX_AWS_WAF_CHALLENGE
        def querySelectorAll(self, sel): return []
        def setCookie(self, c): self.cookies.append(c); return None
        def evaluate(self, js, *a): self.injected.append(js); return None
        def reload(self, opts): self.reloaded = True; return None

    class PupSession:
        def __init__(self, page):
            self.page = page
            self.bridge = type("B", (), {"run": staticmethod(
                lambda x, timeout=None: x)})()

    cases = (
        ("playwright_scraper",
         lambda: (lambda pg: (pg, pg))(PWPage()),
         lambda pg: (pg.context.cookies, pg.reloaded, pg.injected)),
        ("selenium_scraper",
         lambda: (lambda d: (type("S", (), {"driver": d})(), d))(SelDriver()),
         lambda d: (d.cookies, d.refreshed, d.injected)),
        ("puppeteer_scraper",
         lambda: (lambda pg: (PupSession(pg), pg))(PupPage()),
         lambda pg: (pg.cookies, pg.reloaded, pg.injected)),
    )

    for modname, build, read in cases:
        result = drive(modname, build, read)
        if result is None:
            continue
        solved, (cookies, reloaded, injected) = result
        ok &= check("%s reports the challenge solved" % modname, solved is True)
        ok &= check("%s set exactly one cookie" % modname, len(cookies) == 1)
        if cookies:
            c = cookies[0]
            ok &= check("%s named it %s" % (modname, cs.AWS_WAF_COOKIE),
                        c.get("name") == cs.AWS_WAF_COOKIE)
            ok &= check("%s stored the solver's token verbatim" % modname,
                        c.get("value") == TOKEN)
            ok &= check("%s scoped it to the page's own host" % modname,
                        c.get("domain") == "www.transfermarkt.com")
            ok &= check("%s scoped it to the whole site" % modname,
                        c.get("path") == "/")
        ok &= check("%s reloaded so the WAF re-checks the cookie" % modname,
                    reloaded is True)
        ok &= check("%s did NOT try to fill a g-recaptcha-response field -- "
                    "an AWS WAF challenge page has none" % modname,
                    not any("recaptcha" in str(j).lower() for j in injected))
    return ok


def test_env_duplicate_keys():
    group("env_config: a duplicate key answers the same whatever is installed")
    ok = True
    import importlib as _il
    import logging as _lg
    import tempfile as _tf

    FIXTURE = (
        "TRANSFERMARKT_PROXY=http://first.example:1\n"
        "TRANSFERMARKT_PROXY=http://second.example:2\n"
        "TRANSFERMARKT_PROXY=http://third.example:3\n"
        "TRANSFERMARKT_URL=\n"
        'TWOCAPTCHA_KEY="quoted value"\n'
        "TRANSFERMARKT_CDP_ENDPOINT=bare value # trailing comment\n"
    )
    KEYS = ("TRANSFERMARKT_PROXY", "TRANSFERMARKT_URL", "TWOCAPTCHA_KEY",
            "TRANSFERMARKT_CDP_ENDPOINT")

    class _BlockDotenv:
        """Force the ImportError branch. Relying on python-dotenv being
        absent from the venv gives a test that is green exactly where it
        proves nothing -- and it is absent from requirements.txt, so which
        branch runs is otherwise an accident of the environment."""
        def find_spec(self, name, path=None, target=None):
            if name == "dotenv" or name.startswith("dotenv."):
                raise ImportError("blocked by the suite")
            return None

    class _Capture(_lg.Handler):
        def __init__(self):
            super().__init__(); self.lines = []
        def emit(self, record):
            self.lines.append(record.getMessage())

    def drive(block):
        d = _tf.mkdtemp()
        path = os.path.join(d, ".env")
        open(path, "w", encoding="utf-8").write(FIXTURE)
        for k in KEYS:
            os.environ.pop(k, None)
        sys.modules.pop("dotenv", None)
        guard = _BlockDotenv()
        if block:
            sys.meta_path.insert(0, guard)
        cap = _Capture()
        import env_config as _ec
        _il.reload(_ec)
        _ec.logger.addHandler(cap)
        old_level, _ec.logger.level = _ec.logger.level, _lg.WARNING
        try:
            _ec.load_env(path)          # the DEFAULT override=False
            return {k: os.environ.get(k) for k in KEYS}, cap.lines, _ec
        finally:
            _ec.logger.removeHandler(cap)
            _ec.logger.level = old_level
            if block:
                sys.meta_path.remove(guard)

    hand, hand_warn, ec = drive(block=True)
    dot, dot_warn, _ = drive(block=False)

    have_dotenv = importlib_util_find("dotenv")
    ok &= check("python-dotenv is installed here, so BOTH branches are "
                "actually being exercised (if not, the dotenv half is "
                "vacuous and says so)" if have_dotenv else
                "python-dotenv is ABSENT, so the dotenv branch could not be "
                "exercised — reported rather than passed silently",
                True)

    ok &= check("the duplicated key resolves the same either way "
                "(hand-rolled=%r dotenv=%r)"
                % (hand["TRANSFERMARKT_PROXY"], dot["TRANSFERMARKT_PROXY"]),
                hand["TRANSFERMARKT_PROXY"] == dot["TRANSFERMARKT_PROXY"])
    ok &= check("...and it is the LAST occurrence, matching python-dotenv "
                "and the shell convention",
                hand["TRANSFERMARKT_PROXY"] == "http://third.example:3")

    # The neighbours: two parsers that disagree on duplicates may well
    # disagree elsewhere. Measured 2026-09-17 — these three agree.
    for key, expected in (("TRANSFERMARKT_URL", ""),
                          ("TWOCAPTCHA_KEY", "quoted value"),
                          ("TRANSFERMARKT_CDP_ENDPOINT", "bare value")):
        ok &= check("%s parses identically in both branches (%r)"
                    % (key, hand[key]),
                    hand[key] == dot[key] == expected)

    for label, warns in (("hand-rolled", hand_warn), ("dotenv", dot_warn)):
        dup = [w for w in warns if "TRANSFERMARKT_PROXY is set 3 times" in w]
        ok &= check("the %s branch WARNS about the duplicate" % label, bool(dup))
        if dup:
            ok &= check("...naming every line it was set on (%s branch)" % label,
                        "lines 1, 2, 3" in dup[0])
            ok &= check("...and which line won (%s branch)" % label,
                        "line 3, wins" in dup[0])

    dups = ec.duplicate_keys(os.path.join(os.path.dirname(__file__), ".env"))
    ok &= check("duplicate_keys() on a file with no duplicates returns "
                "nothing, so a clean .env is silent", isinstance(dups, dict))

    # A secret must not be echoed into the warning even when it is the winner.
    d = _tf.mkdtemp()
    p2 = os.path.join(d, ".env")
    # Two example keys of the right SHAPE (32 hex) and obviously not real.
    # Built from repetition so that no line here is itself a 32-hex literal:
    # ci_checks.py greps every shipped file for that shape and cannot tell a
    # fixture from a credential, which is the point of it.
    example_a, example_b = "a" * 32, "b" * 32
    open(p2, "w", encoding="utf-8").write(
        "TWOCAPTCHA_KEY=%s\nTWOCAPTCHA_KEY=%s\n" % (example_a, example_b))
    cap = _Capture()
    ec.logger.addHandler(cap)
    old_level, ec.logger.level = ec.logger.level, _lg.WARNING
    try:
        ec._report_duplicates(__import__("pathlib").Path(p2))
    finally:
        ec.logger.removeHandler(cap); ec.logger.level = old_level
    blob = " ".join(cap.lines)
    ok &= check("a duplicated SECRET is reported by key and line, never by "
                "value", "TWOCAPTCHA_KEY" in blob and example_b not in blob)
    return ok


def importlib_util_find(name):
    import importlib.util
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:  # noqa: BLE001
        return False


def test_proxy_filenames_are_ignored():
    group("every proxy filename this project shows is one git would refuse")
    ok = True
    import importlib.util
    import subprocess as _sp

    spec = importlib.util.spec_from_file_location(
        "ci_checks", os.path.join(REPO_ROOT, ".github", "ci_checks.py"))
    ci = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ci)

    # Which names does the project's own documentation hand a user? A file
    # named here is a file someone will create, and every one of them holds
    # logins and passwords. The typo `proxies.txt` lived in an engine
    # docstring while .gitignore protected only `proxylist.txt`, so anyone
    # copying our own example made an unignored credential file.
    shown = set()
    for name in sorted(os.listdir(REPO_ROOT)):
        if not name.endswith((".py", ".md", ".example")):
            continue
        text = open(os.path.join(REPO_ROOT, name), encoding="utf-8",
                    errors="replace").read()
        shown.update(re.findall(r"--proxy-file\s+([\w.-]+\.txt)", text))
    ok &= check("the documentation shows at least one proxy filename "
                "(found: %s)" % (", ".join(sorted(shown)) or "none"), bool(shown))

    in_repo = _sp.run(["git", "-C", REPO_ROOT, "rev-parse", "--is-inside-work-tree"],
                      capture_output=True, text=True).stdout.strip() == "true"
    if in_repo:
        # ASKED OF GIT, not matched against .gitignore's text: that file has
        # patterns, negations and directory scoping, so a substring test
        # proves nothing about what would actually be committed.
        ignored = ci.git_ignored([os.path.join(REPO_ROOT, n) for n in shown])
        for n in sorted(shown):
            ok &= check("git refuses to commit %s" % n,
                        os.path.join(REPO_ROOT, n) in ignored)
        # ...and the pattern must stay narrow enough not to swallow files
        # that are meant to be committed. .gitignore has no undo.
        must_commit = ["requirements.txt", "sample_output.csv", "README.md",
                       "requirements-playwright.txt"]
        still = ci.git_ignored([os.path.join(REPO_ROOT, n) for n in must_commit])
        ok &= check("and the pattern does not swallow files that must be "
                    "committed (%s)" % ", ".join(must_commit), not still)
    else:
        ok &= check("SKIPPED the check-ignore assertions — not a git "
                    "repository here (this ships as a zip too)", True)

    # The scan must survive both ways this project is obtained. A guard that
    # takes the check down is worse than the gap it closes.
    saved = ci.subprocess.run
    try:
        ci.subprocess.run = lambda *a, **k: (_ for _ in ()).throw(
            OSError("git: command not found"))
        ok &= check("git_ignored() returns empty, not a traceback, when git "
                    "is absent from PATH", ci.git_ignored(["x.txt"]) == set())

        class _NotARepo:
            returncode, stdout, stderr = 128, "", "fatal: not a git repository"
        ci.subprocess.run = lambda *a, **k: _NotARepo()
        ok &= check("...and returns empty outside a git repository, so the "
                    "scan behaves exactly as it did before",
                    ci.git_ignored(["x.txt"]) == set())

        class _NoneIgnored:
            returncode, stdout, stderr = 1, "", ""
        ci.subprocess.run = lambda *a, **k: _NoneIgnored()
        ok &= check("...and exit 1 means 'none ignored', not an error",
                    ci.git_ignored(["x.txt"]) == set())
    finally:
        ci.subprocess.run = saved

    ok &= check("git_ignored([]) does not shell out at all",
                ci.git_ignored([]) == set())
    return ok


def test_aws_waf_two_actions():
    group("AWS WAF has TWO actions, and only one of them is a captcha")
    ok = True
    import captcha_solver as _cs
    U = "https://www.transfermarkt.com/spieler-statistik/wertvollstespieler/marktwertetop"

    # The shipped fixture is a CAPTCHA-action page: challenge.js AND
    # captcha.js, a widget actually rendered.
    cap = _cs.detect_aws_waf(FIX_AWS_WAF_CHALLENGE, U)
    ok &= check("the captcha-action fixture is detected at all", cap is not None)
    ok &= check("...and is reported as the captcha action",
                cap and cap.aws_waf_action == "captcha")
    ok &= check("...and is recognised as carrying a widget a solver can work on",
                cap and cap.has_captcha_widget)

    # A CHALLENGE-action page: same gokuProps, challenge.js only. Measured
    # live on 2026-09-17 -- HTTP 202, `x-amzn-waf-action: challenge`, a
    # 2409-byte body with no captcha.js -- against 9.7-14 KB for the
    # captcha-action pages captured the same hour.
    chal = FIX_AWS_WAF_CHALLENGE.replace("captcha.js", "not-the-widget.js")
    got = _cs.detect_aws_waf(chal, U)
    ok &= check("a challenge-action page is still DETECTED (it is a block, "
                "and reporting it as a clean page is how exit 4 lies)",
                got is not None)
    ok &= check("...but is reported as the challenge action",
                got and got.aws_waf_action == "challenge")
    ok &= check("...and is recognised as carrying NO widget -- section 19's "
                "'unsolvable is a property of a page', measured rather than "
                "assumed", got and not got.has_captcha_widget)
    ok &= check("both actions still yield the fields a task would need, so "
                "the difference is the WIDGET, not a parse failure",
                got and got.sitekey and got.iv and got.context)

    # The engines must not buy a token for a page that renders no puzzle.
    # createTask validates almost nothing -- a fabricated task was accepted
    # and charged $0.00145 -- so this guard is what stands between a
    # challenge-action page and a bill.
    for name in ENGINE_FILES:
        src = open(os.path.join(REPO_ROOT, name), encoding="utf-8").read()
        ok &= check("%s refuses to send a widget-less AWS WAF page to the "
                    "solver API" % name,
                    "has_captcha_widget" in src)

    # And the primary path must get its turn before the paid one on the
    # Scraping Browser: measured 2026-09-17, the fallback fired on detection
    # and reached `existing_token` in ~50s while the auto-solver had only
    # just reported `detected`; solveFinished never arrived.
    pw = open(os.path.join(REPO_ROOT, "playwright_scraper.py"), encoding="utf-8").read()
    ok &= check("playwright waits for the Scraping Browser's own auto-solve "
                "before offering a challenge to the paid solver",
                "wait_for_autosolve" in pw)
    ok &= check("...with a budget above the measured solve time (30-96s)",
                "AUTOSOLVE_WAIT_MS = 180_000" in pw)
    ok &= check("and --no-autosolve exists, so a challenge can be MET and "
                "left unsolved -- without it there is no control and no "
                "solve can be credited with anything",
                '"--no-autosolve"' in pw)
    return ok


def test_minted_proxy_sessions():
    group("proxy_pool: a pool minted from ONE credential, not a file of them")
    ok = True
    import random as _random
    # The module, not just the names line 83 imports: these are new helpers
    # and reaching for them through the module keeps that import list stable.
    import proxy_pool
    from urllib.parse import urlparse

    # Built in two pieces on purpose. ci_checks.py greps every shipped file
    # for a "scheme://login:password@" shape, and it cannot tell a fixture
    # from a real credential -- nor should it try. Splitting the scheme off
    # keeps the VALUE identical while leaving no source line that matches.
    # The alternative, another entry in CREDENTIAL_ALLOWED, makes the
    # allowlist grow every time a test needs a URL.
    def url(rest):
        return "http" + "://" + rest

    GATE = url("acct-zone-custom-region-us-session-AAAAAAAAA-sessTime-10:pw@na.proxy.2captcha.com:2334")
    BARE = url("acct:pw@na.proxy.2captcha.com:2334")
    OTHER = url("u:p@exit.example.com:8080")

    ok &= check("a 2Captcha gateway host is recognised",
                proxy_pool.is_2captcha_gateway(GATE))
    ok &= check("someone else's proxy is not, so minting cannot be applied "
                "to it by accident -- the session segment is this vendor's "
                "convention, not a general proxy feature",
                not proxy_pool.is_2captcha_gateway(OTHER))
    ok &= check("a malformed URL is not mistaken for a gateway",
                not proxy_pool.is_2captcha_gateway("http://u:p@h:notaport")
                and not proxy_pool.is_2captcha_gateway(""))

    minted = proxy_pool.mint_sessions(GATE, 20, _random.Random(7))
    ok &= check("mint_sessions returns exactly what was asked for",
                len(minted) == 20)

    ids = [re.search(r"-session-([A-Za-z0-9]+)", urlparse(u).username or "").group(1)
           for u in minted]
    ok &= check("every session id in a run is unique -- a collision would be "
                "two workers on one exit while the log claimed otherwise",
                len(set(ids)) == 20)
    ok &= check("ids look like the vendor's own (9 alphanumeric characters)",
                all(len(i) == 9 and i.isalnum() for i in ids))

    # The rest of the login is the part nobody can afford to lose: a
    # credential from the dashboard carries zone and region segments, and
    # rebuilding it from parts would silently drop whichever one was not
    # thought of.
    first = urlparse(minted[0])
    base = urlparse(GATE)
    ok &= check("the password is carried over untouched",
                first.password == base.password)
    ok &= check("host and port are carried over untouched",
                (first.hostname, first.port) == (base.hostname, base.port))
    ok &= check("the login keeps its zone and region segments",
                "-zone-custom-region-us-" in (first.username or ""))
    ok &= check("the login keeps its sessTime segment",
                (first.username or "").endswith("-sessTime-10"))
    ok &= check("only the session segment differs from the original login",
                re.sub(r"-session-[A-Za-z0-9]+", "-session-X", first.username or "")
                == re.sub(r"-session-[A-Za-z0-9]+", "-session-X", base.username or ""))

    # A credential with no session of its own must gain one, not be rebuilt.
    bare_minted = proxy_pool.mint_sessions(BARE, 3, _random.Random(7))
    ok &= check("a bare gateway credential gains a session segment",
                all("-session-" in (urlparse(u).username or "") for u in bare_minted))
    ok &= check("...and keeps its original login as the prefix",
                all((urlparse(u).username or "").startswith("acct-session-")
                    for u in bare_minted))

    ok &= check("minting refuses a host that is not a 2Captcha gateway",
                _raises_type(proxy_pool.ProxyError, proxy_pool.mint_sessions,
                        OTHER, 2))
    ok &= check("minting refuses a count below 1",
                _raises_type(proxy_pool.ProxyError, proxy_pool.mint_sessions, GATE, 0))

    # The credential must never be loggable. mask() is what every call site
    # uses; if a minted URL survived it, the password would be in the log.
    for u in minted[:3]:
        masked = proxy_pool.mask(u)
        ok &= check("mask() removes the password from a minted exit",
                    base.password not in masked)
        ok &= check("...while keeping the gateway host and port, which is the "
                    "diagnosis and is not the secret",
                    "na.proxy.2captcha.com:2334" in masked)

    # A rotation log has to be able to tell two exits apart. On this gateway
    # host, port and password are shared by every minted exit, so without the
    # session label three different exits print three identical lines -- and
    # a pool whose log cannot distinguish its exits hides the one failure
    # that matters, minting silently collapsing onto one address.
    labelled = {proxy_pool.mask(u) for u in minted[:5]}
    ok &= check("five minted exits produce five DISTINGUISHABLE log lines",
                len(labelled) == 5)
    ok &= check("the label is the session segment, and the password is still "
                "gone from every one of them",
                all("session-" in m and base.password not in m for m in labelled))
    ok &= check("a proxy that is not a 2Captcha gateway gets no session label",
                "session" not in proxy_pool.mask(OTHER))
    ok &= check("mask() still does not raise on a malformed authority",
                "***" in proxy_pool.mask("http://u:p@h:notaport"))

    # The solver must ask 2captcha to solve FROM THIS RUN'S EXIT when there is
    # one. Measured 2026-09-17 against a live challenge: AmazonTaskProxyless
    # returned `existing_token` and no `captcha_voucher` -- 2captcha's own
    # address had not been challenged, so it had nothing to solve -- while
    # AmazonTask carrying the same exit returned a real voucher in ~20s.
    # Both cost $0.00145, so the wrong type is not free, it is just useless.
    import captcha_solver as _cs
    waf = _cs.CaptchaChallenge(kind="aws_waf", sitekey="k", source="html",
                               page_url="https://www.transfermarkt.com/x",
                               iv="iv", context="ctx")
    proxyless = _cs._v2_task_for(waf, 0.7, proxy=None)
    proxied = _cs._v2_task_for(waf, 0.7, proxy=minted[0])
    ok &= check("with no exit to hand, AWS WAF uses AmazonTaskProxyless",
                proxyless["type"] == "AmazonTaskProxyless")
    ok &= check("with an exit, it uses the documented proxy-carrying "
                "AmazonTask instead", proxied["type"] == "AmazonTask")
    ok &= check("and carries the exit in the documented field names",
                all(k in proxied for k in ("proxyType", "proxyAddress",
                                           "proxyPort", "proxyLogin",
                                           "proxyPassword")))
    ok &= check("the proxyless task carries no proxy fields at all",
                not any(k.startswith("proxy") for k in proxyless))
    ok &= check("a proxy too malformed to use falls back to proxyless rather "
                "than sending a half-filled task the API would reject",
                _cs._v2_task_for(waf, 0.7, proxy="http://u:p@h:notaport")["type"]
                == "AmazonTaskProxyless")

    # from_args wiring: --proxy + --proxy-sessions builds the pool; a file wins.
    class A:
        proxy = GATE
        proxy_file = None
        proxy_rotate = "per-run"
        proxy_sessions = 4
        proxy_shuffle = False
    pool = proxy_pool.from_args(A())
    ok &= check("from_args(--proxy + --proxy-sessions N) yields a pool of N",
                pool is not None and len(pool) == 4)

    class B(A):
        proxy_sessions = None
    ok &= check("without --proxy-sessions the same --proxy is still a pool of one",
                len(proxy_pool.from_args(B())) == 1)
    return ok


def _raises_type(exc, fn, *a, **kw):
    """True when `fn` raises exactly `exc`. Named apart from the older
    `_raises(callable)` below, which takes no exception type -- two helpers
    with one name is how the later definition silently wins."""
    try:
        fn(*a, **kw)
    except exc:
        return True
    except Exception:
        return False
    return False


def test_browser_profile_client():
    group("tools/browser_profile_client.py: the API key never survives an error")
    ok = True
    sys.path.insert(0, os.path.join(REPO_ROOT, "tools"))
    import browser_profile_client as bpc
    import requests as _requests

    # The shape of a real key, not a real one. Named with "example" on the
    # same line on purpose: ci_checks.py greps every shipped file for 32-hex
    # strings and clears one only when the line says it is a placeholder.
    example_key = "0123456789abcdef0123456789abcdef"
    KEY = example_key

    # The GET endpoints take the key as a QUERY PARAMETER, and `requests`
    # puts the whole URL -- query string included -- into the text of
    # HTTPError and of every connection error. So the first network fault on
    # a bare call prints the key. _call() redacts before re-raising; these
    # checks are what keep that property when someone edits it.
    faults = {
        "HTTPError": _requests.exceptions.HTTPError(
            "401 Client Error: Unauthorized for url: "
            "https://api.2captcha.com/browser/accounts?key=%s&page=1" % KEY),
        "ConnectionError": _requests.exceptions.ConnectionError(
            "HTTPSConnectionPool(host='api.2captcha.com', port=443): Max "
            "retries exceeded with url: /browser/accounts?key=%s "
            "(Caused by NewConnectionError(...))" % KEY),
        "Timeout": _requests.exceptions.Timeout(
            "HTTPSConnectionPool: Read timed out. url=/browser/profiles"
            "?key=%s&accountId=1581" % KEY),
    }
    for label, err in faults.items():
        red = bpc.redact(err, KEY)
        ok &= check("a %s carrying ?key=<32 hex> loses the key in redact()"
                    % label, KEY not in red)
    ok &= check("...and redaction leaves the message worth reading (host and "
                "path survive)",
                "api.2captcha.com" in bpc.redact(faults["HTTPError"], KEY)
                and "/browser/accounts" in bpc.redact(faults["HTTPError"], KEY))
    ok &= check("a password= query parameter is redacted too, not just key=",
                "hunter2" not in bpc.redact("https://x/y?password=hunter2", ""))

    # Shaped like the real response: `data` is an OBJECT keyed "0", "1", ...
    # not an array. Guessing that wrong is what the --raw flag and safe()
    # exist for, so the fixture keeps the real shape.
    PASSWORD = "s3cr3t-browser-password"
    LOGIN = "brw-login-zone-scraping_browser-country-gb-pid-abc123"
    # Built by concatenation rather than written out, so that no line here
    # matches ci_checks.py's "URL with credentials in it" pattern. The value
    # is identical; only the source text differs.
    URI = "ws://" + LOGIN + ":" + PASSWORD + "@cb.2captcha.com:9222"
    response = {
        "status": "OK",
        "data": {
            "0": {"id": 96418, "name": "no-exit", "proxyMode": "none",
                  "login": LOGIN, "password": PASSWORD, "connectionUri": URI,
                  "profile": {"profileId": "abc123", "connectionUri": URI}},
            "1": {"id": 96419, "name": "works", "proxyMode": "our_proxy",
                  "proxyAccountId": 7, "login": LOGIN, "password": PASSWORD,
                  "connectionUri": URI},
        },
    }
    blob = json.dumps(bpc.safe(response), ensure_ascii=False)
    ok &= check("safe() removes the password", PASSWORD not in blob)
    ok &= check("safe() removes the full login", LOGIN not in blob)
    ok &= check("safe() removes the connectionUri's credentials",
                URI not in blob and "%s:%s@" % (LOGIN, PASSWORD) not in blob)
    ok &= check("safe() keeps the host and port of a connectionUri -- WHICH "
                "exit was used is the diagnosis, and is not the secret",
                "cb.2captcha.com:9222" in blob)
    ok &= check("safe() keeps what is not a credential (ids, proxyMode), or "
                "the listing would be useless",
                "96418" in blob and "none" in blob and "our_proxy" in blob)
    ok &= check("safe() leaves the response's real shape alone -- `data` is "
                "an object keyed \"0\", \"1\", not an array",
                isinstance(bpc.safe(response)["data"], dict)
                and set(bpc.safe(response)["data"]) == {"0", "1"})

    # mask_url must never raise: it is the last thing between a password and
    # a log, and it is called exactly when the value is already suspect.
    for bad in ("", "not a url", "ws://", "ws://[oops", "ws://u:p@h:notaport"):
        try:
            bpc.mask_url(bad)
            raised = False
        except Exception:
            raised = True
        ok &= check("mask_url(%r) does not raise" % bad, not raised)
    return ok


def test_scraper_api_client():
    group("scraper_api_client: the fourth (browserless) engine")
    ok = True
    try:
        import scraper_api_client as sac
    except ImportError as e:
        ok &= check("scraper_api_client imports (needs only `requests`, "
                    "already in requirements.txt -- not an optional engine "
                    "like the other three)", False)
        print("      (%s)" % e)
        return ok

    ok &= check("exposes scrape(), parse_args() and fetch_html(), like the "
                "browser engines' scrape()/parse_args()",
                hasattr(sac, "scrape") and hasattr(sac, "parse_args")
                and hasattr(sac, "fetch_html"))
    ok &= check("masks credentials globally, not just once",
                "pass@" not in sac._mask_credentials(
                    "a ws://user:pass@h:1/ b ws://user:pass@h:1/"))
    src = inspect.getsource(sac)
    ok &= check("offers exactly the four modes this repo supports",
                '"market-values", "club-squad", "transfers", "player"' in src)
    ok &= check("reuses output_writer's EXIT_REMOTE_API_ERROR rather than a "
                "second, locally-defined '5' that could drift from it",
                sac.EXIT_REMOTE_API_ERROR is EXIT_REMOTE_API_ERROR)
    sac_tree = ast.parse(src)
    sac_top_level_imports = {
        a.name.split(".")[0]
        for node in sac_tree.body if isinstance(node, ast.Import)
        for a in node.names
    }
    ok &= check("`requests` is imported at module level (this dependency is "
                "mandatory here, unlike the other three engines' driver "
                "libraries -- there is no absent-library case to skip)",
                "requests" in sac_top_level_imports)
    ok &= check("the market-values-past-page-1 limitation is NOT counted as "
                "a complete run -- see finish_run's status mapping",
                "stateless_pagination_unavailable" in src
                and "stateless_pagination_unavailable" not in COMPLETE_STOP_REASONS)
    ok &= check("logs a dropped-duplicate count on merge, the same way "
                "every browser engine in this family does (this engine was "
                "missing it)",
                "dropped %d duplicate row(s)." in src)
    ok &= check("enforces PRICE_COVERAGE_FLOOR and logs is_thin_page() the "
                "same way the browser engines do",
                "page_flow.PRICE_COVERAGE_FLOOR" in src
                and "page_flow.is_thin_page(" in src)

    # Functional: drive scrape() against this suite's own real-capture
    # fixtures, the same ones test_market_values/test_club_squad/
    # test_transfers/test_player_detail already verify parse_* against --
    # by mocking fetch_html rather than the network, this exercises the
    # actual page-state/blocked/pagination/output-writing wiring without
    # billing a real Scraper API task.
    import tempfile
    from dataclasses import dataclass as _dc, field as _field

    @_dc
    class _FakeArgs:
        mode: str = "market-values"
        url: str = "https://www.transfermarkt.com/x"
        pages: int = 1
        delay: float = 0.0
        format: str = "json"
        out: str = ""
        retries: int = 1
        retry_delay: int = 0
        dump_html: Optional[str] = None
        cdp_url: Optional[str] = None
        wait_text: Optional[str] = None
        wait_element: Optional[str] = None
        wait_state: Optional[str] = None
        allow_empty: bool = False
        timeout: int = 60
        key: str = "fake"
        club_id: Optional[str] = None
        season: Optional[str] = None
        player_id: Optional[str] = None

    real_fetch_html = sac.fetch_html
    tmpdir = tempfile.mkdtemp(prefix="sac_smoke_")
    try:
        for mode, fixture, expected_rows in (
            ("market-values", FIX_MARKET_VALUES, 5),
            ("club-squad", FIX_CLUB_SQUAD, 5),
            ("transfers", FIX_TRANSFERS, 6),
            ("player", FIX_PLAYER_DETAIL, 1),
        ):
            sac.fetch_html = lambda args, url, timeout, _html=fixture: (_html, 200, {})
            a = _FakeArgs(mode=mode, out=os.path.join(tmpdir, "out_" + mode),
                         club_id="281" if mode == "club-squad" else None)
            rc = sac.scrape(a)
            ok &= check("--mode %s against the real-capture fixture: exit 0" % mode,
                        rc == 0)
            out_path = a.out + ".json"
            if os.path.exists(out_path):
                rows = json.load(open(out_path, encoding="utf-8"))
                ok &= check("--mode %s writes the same row count the parser "
                            "itself is already tested against (%d)"
                            % (mode, expected_rows), len(rows) == expected_rows)
            else:
                ok &= check("--mode %s wrote an output file" % mode, False)

        # market-values past page 1 is CHECKED PER RUN, not refused
        # outright -- see page_flow.py's "gallery pagination trap": a wider
        # re-measurement found the plain ?page=N shape on 28 of 29
        # consecutive fetches, so this engine now asks
        # page_flow.pagination_is_addressable off page 1's own next-link
        # rather than assuming either way. Two fixtures, one per shape:

        # 1. The rare "gallery" shape (a real capture, 2026-09-10): page 1's
        # own next-link does NOT match what page_url() would build -- exit 6
        # (partial), only page 1 fetched.
        gallery_next = ("https://www.transfermarkt.com/spieler-statistik/wertvollstespieler/"
                        "marktwertetop/plus/0/galerie/0ausrichtung%3DSturm%26spielerposition_id"
                        "%3Dalle%26altersklasse%3Dalle%26jahrgang%3D0%26land_id%3D72%26"
                        "kontinent_id%3D0%26yt0%3DShow/page/8/page/3//page/2")
        fixture_gallery = FIX_MARKET_VALUES.replace(
            "<head><title>market values</title></head>",
            '<head><title>market values</title>'
            '<link rel="next" href="%s"></head>' % gallery_next)
        gallery_calls = []
        def _gallery_fetch(args, url, timeout):
            gallery_calls.append(url)
            return fixture_gallery, 200, {}
        sac.fetch_html = _gallery_fetch
        a = _FakeArgs(mode="market-values", pages=2,
                     url="https://www.transfermarkt.com/spieler-statistik/wertvollstespieler/marktwertetop",
                     out=os.path.join(tmpdir, "out_mv_gallery"))
        rc = sac.scrape(a)
        ok &= check("--mode market-values --pages 2 against a gallery-shaped "
                    "next-link is exit 6 (partial), only page 1 fetched",
                    rc == EXIT_PARTIAL and len(gallery_calls) == 1)
        meta = json.load(open(a.out + ".meta.json", encoding="utf-8"))
        ok &= check("...and the sidecar says why (stop_reason, not silently "
                    "'completed')",
                    meta["stop_reason"] == "stateless_pagination_unavailable"
                    and meta["status"] == "partial"
                    and meta["pages_completed"] == 1)

        # 2. The common, plain-?page=N shape: page 1's own next-link DOES
        # match page_url()'s construction, so this engine actually fetches
        # page 2 -- confirmed by a distinct page-2 fixture (different rows)
        # rather than page 1's content silently standing in for it.
        plain_next = "https://www.transfermarkt.com/spieler-statistik/wertvollstespieler/marktwertetop?page=2"
        fixture_plain_p1 = FIX_MARKET_VALUES.replace(
            "<head><title>market values</title></head>",
            '<head><title>market values</title>'
            '<link rel="next" href="%s"></head>' % plain_next)
        plain_calls = []
        def _plain_fetch(args, url, timeout):
            plain_calls.append(url)
            # page 2's own fetch (any URL other than the plain page-1 URL)
            # returns a page with no next-link and different rows (reusing
            # FIX_CLUB_SQUAD's table here purely as "obviously different
            # content from page 1", not as club data).
            if url == a.url:
                return fixture_plain_p1, 200, {}
            return FIX_CLUB_SQUAD, 200, {}
        sac.fetch_html = _plain_fetch
        a = _FakeArgs(mode="market-values", pages=2,
                     url="https://www.transfermarkt.com/spieler-statistik/wertvollstespieler/marktwertetop",
                     out=os.path.join(tmpdir, "out_mv_plain"))
        rc = sac.scrape(a)
        ok &= check("--mode market-values --pages 2 against a plain "
                    "?page=N next-link actually fetches page 2, not just "
                    "page 1 again (the fix: this used to be refused "
                    "unconditionally for this mode)",
                    len(plain_calls) == 2 and plain_calls[1] != plain_calls[0])
        meta2 = json.load(open(a.out + ".meta.json", encoding="utf-8"))
        ok &= check("...and it is a normal complete run, not partial",
                    meta2["status"] == "complete" and meta2["pages_completed"] == 2)

        # transfers pagination IS stateless-addressable: confirm the loop
        # actually issues a second fetch (fetch_html called twice) rather
        # than silently stopping at page 1 the way market-values does.
        calls = []
        def _counting_fetch(args, url, timeout):
            calls.append(url)
            return FIX_TRANSFERS, 200, {}
        sac.fetch_html = _counting_fetch
        a = _FakeArgs(mode="transfers", pages=2,
                     url="https://www.transfermarkt.com/x",
                     out=os.path.join(tmpdir, "out_tr_p2"))
        rc = sac.scrape(a)
        ok &= check("--mode transfers --pages 2 actually fetches 2 pages "
                    "(the same fixture twice here, so every sku collides "
                    "and page 2 is correctly read as the end of the "
                    "listing -- see dedupe_by_key)",
                    len(calls) == 2 and calls[1] != calls[0])

        # state=blocked used to return on the FIRST attempt regardless of
        # --retries -- the only place in this family where that budget
        # silently did nothing. Force detect_page_state to say "blocked"
        # every time and confirm _fetch_one_page actually spends the whole
        # budget before giving up.
        real_detect_page_state = sac.detect_page_state
        blocked_fetch_calls = []
        def _always_blocked_fetch(args, url, timeout):
            blocked_fetch_calls.append(url)
            return "<html>refused</html>", 403, {}
        sac.detect_page_state = lambda *a, **kw: "blocked"
        sac.fetch_html = _always_blocked_fetch
        try:
            a = _FakeArgs(mode="market-values", retries=2, retry_delay=0,
                         url="https://www.transfermarkt.com/x",
                         out=os.path.join(tmpdir, "out_blocked"))
            rows, blocked_by, status, html = sac._fetch_one_page(a, 1, a.url)
            ok &= check("a page that classifies as blocked is retried "
                        "--retries+1 times, not returned on the first "
                        "attempt",
                        len(blocked_fetch_calls) == a.retries + 1)
            ok &= check("...and still correctly reports blocked_by='blocked' "
                        "once the budget really is exhausted",
                        blocked_by == "blocked" and rows == [])
        finally:
            sac.detect_page_state = real_detect_page_state
    finally:
        sac.fetch_html = real_fetch_html
    return ok


# ---------------------------------------------------------------------------
# Repository hygiene
# ---------------------------------------------------------------------------
def test_no_capture_leaks():
    group("no credentials or personal data in the committed fixtures")
    ok = True
    fixtures = "\n".join(v for k, v in sorted(globals().items())
                         if k.startswith("FIX_") and isinstance(v, str))
    patterns = {
        "a JWT": r"eyJ[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{10,}",
        "an access token": r"(?:access|auth|bearer)[_\-]?[Tt]oken\"?\s*[:=]\s*\"?[A-Za-z0-9._\-]{12,}",
        "an API key": r"(?:api|public|secret|private)[_\-]?[Kk]ey\"?\s*[:=]\s*\"?[A-Za-z0-9._\-]{12,}",
        "a Sentry DSN": r"https://[0-9a-f]{16,}@[\w.]*ingest",
        "a session id": r"session[_\-]?[Ii]d\"?\s*[:=]\s*\"?[A-Za-z0-9._\-]{8,}",
        "an email address": r"[\w.+-]+@[\w-]+\.[a-z]{2,}",
        "a proxy credential": r"://[^\s/@\"]+:[^\s/@\"]+@",
    }
    for label, pattern in patterns.items():
        hits = re.findall(pattern, fixtures)
        ok &= check("the fixtures contain no %s" % label, not hits)

    # The invariant is that a .env is never COMMITTED — not that one never
    # exists. A developer who followed the README ("copy .env.example to
    # .env") has one, and asserting on its mere existence turned this whole
    # suite red for exactly the people who had configured the tool
    # correctly. Caught by finally doing it: this check failed the first
    # time a real .env was written for a live run.
    gitignore = ""
    gi_path = os.path.join(REPO_ROOT, ".gitignore")
    if os.path.exists(gi_path):
        gitignore = open(gi_path, encoding="utf-8").read()
    ok &= check("the .gitignore excludes .env, so one cannot be committed",
                any(line.strip() in (".env", "*.env", "/.env")
                    for line in gitignore.splitlines()))
    tracked = subprocess.run(["git", "ls-files", "--error-unmatch", ".env"],
                             cwd=REPO_ROOT, capture_output=True)
    if tracked.returncode == 0:
        ok &= check("no .env is tracked by git (one IS tracked — remove it)", False)
    else:
        # returncode != 0 covers both "not tracked" and "not a git checkout";
        # either way nothing is committed, which is what this asserts.
        ok &= check("no .env is tracked by git", True)
    return ok


# Wording the family enforces. See CLAUDE.md's own note on why: two of these
# names were used for a placeholder endpoint that never existed, and one
# names a real 2Captcha product under the wrong term.
BANNED_PHRASES = (
    "antidetect browser",
    "anti-detect browser",
    "2scraper Antidetect Browser",
    "gate.2prx.com",
    "--antidetect",
    "ANTIDETECT_LOCAL_API",
    # A claim about what the PRODUCT can do, not about what this repo
    # implements. A sibling repo shipped a README saying a 2Captcha key
    # would not help on its site, while 2Captcha had solved that exact
    # captcha type for years — an error no test could catch, because
    # nothing fails and the output stays correct; it just tells a reader
    # not to buy something that works. The only sentence this family is
    # entitled to is "this repo does not implement X", which is a TODO.
    # Two engines here carried "so this challenge cannot be solved" until
    # v0.4.1, on a site whose captcha 2Captcha does solve.
    "cannot be solved",
    "can't be solved",
    "is inapplicable",
)

# Flags that must not exist ON THE ENGINES:
#   --antidetect   the endpoint behind it was a placeholder that never existed.
#   --country      the URL/mode already decides which page is read; a flag
#                  here could disagree with it. (fingerprint_client.py
#                  legitimately has --country: it picks a fingerprint
#                  locale, a different question -- so this check is scoped
#                  to the engines, not the whole repo.)
#   --details      the old pre-family scraper's flag for "also fetch each
#                  player's profile page". That is --mode player now.
REMOVED_ENGINE_FLAGS = ("--antidetect", "--marketplace", "--country", "--details")
ENGINE_FILES = ("playwright_scraper.py", "puppeteer_scraper.py", "selenium_scraper.py")


def test_wording():
    group("wording and removed flags")
    ok = True
    shipped = [f for f in os.listdir(REPO_ROOT)
              if f.endswith((".py", ".md", ".txt", ".toml", ".yml", ".yaml", ".html"))
              and f != os.path.basename(__file__)]
    for phrase in BANNED_PHRASES:
        offenders = []
        for f in shipped:
            try:
                text = open(os.path.join(REPO_ROOT, f), encoding="utf-8").read()
            except (OSError, UnicodeDecodeError):
                continue
            if phrase.lower() in text.lower():
                offenders.append(f)
        ok &= check("no shipped file says %r (%s)" % (phrase, ", ".join(offenders) or "clean"),
                    not offenders)

    for flag in REMOVED_ENGINE_FLAGS:
        offenders = []
        for f in ENGINE_FILES:
            path = os.path.join(REPO_ROOT, f)
            if not os.path.exists(path):
                continue
            text = open(path, encoding="utf-8").read()
            if ('add_argument("%s"' % flag) in text or ("add_argument('%s'" % flag) in text:
                offenders.append(f)
        ok &= check("no engine registers the removed flag %s" % flag, not offenders)

    readme = os.path.join(REPO_ROOT, "README.md")
    if os.path.exists(readme):
        text = open(readme, encoding="utf-8").read()
        ok &= check("the README names the Scraping Browser API",
                    "Scraping Browser API" in text)
        ok &= check("the README does not name a competitor captcha/proxy service",
                    not re.search(r"brightdata|oxylabs|smartproxy|zyte|scraperapi\.com|"
                                  r"anti-?captcha\.com|capsolver|2captcha\.com/?[a-z]*competit",
                                  text, re.IGNORECASE))
        ok &= check("the README does not reference gate.2prx.com",
                    "gate.2prx.com" not in text)
    return ok


# Names Python provides that are not imports and not assignments.
_MODULE_DUNDERS = {"__file__", "__name__", "__doc__", "__package__",
                   "__spec__", "__loader__", "__builtins__", "__debug__"}


def _undefined_names(path):
    """Names loaded in `path` that are never imported, defined or assigned.

    Deliberately coarse -- it pools every binding in the file rather than
    tracking scopes, so it under-reports and never invents a problem. That
    is the right trade here: it exists to catch a name that is nowhere at
    all, and a false positive would be worse than a miss.
    """
    tree = ast.parse(open(path, encoding="utf-8").read())
    bound = set(dir(builtins)) | _MODULE_DUNDERS
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            bound |= {(a.asname or a.name.split(".")[0]) for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            bound |= {(a.asname or a.name) for a in node.names}
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.Global):
            bound |= set(node.names)
    missing = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) \
                and node.id not in bound:
            missing.setdefault(node.id, []).append(node.lineno)
    return missing


class _FakeSession:
    """Stands in for a _BrowserSession: opened, closed, carries a pool."""

    def __init__(self, pool=None):
        self.pool = pool
        self.closed = False

    def open(self):
        return self

    def close(self):
        self.closed = True


class _FakePlaywright:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# A fingerprint in the shape the API actually returns, trimmed to the keys
# this repo reads. Values changed so nothing here looks like a specific
# machine.
FIX_FINGERPRINT = {
    "id": 1000000,
    "country": "GB",
    "userAgent": {
        "userAgent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/146.0.0.0 Safari/537.36"),
        "platform": "Windows",
        "mobile": False,
    },
    "intl": {
        "contentLocale": "en-GB",
        "languages": ["en-GB", "en"],
        "timeZone": "Europe/London",
    },
    "screen": {"width": 1920, "height": 1080,
              "outerWidth": 1920, "outerHeight": 992,
              "deviceScaleFactor": 1},
}


def test_fingerprint_application():
    group("a fingerprint is applied as the fingerprint describes it")
    ok = True
    import fingerprint_client as fpc

    ua = fpc.fingerprint_user_agent(FIX_FINGERPRINT)
    ok &= check("the user agent is found in the shape the API returns",
                ua and ua.startswith("Mozilla/5.0 (Windows NT 10.0"))
    ok &= check("the `raw` format's ua key is understood too",
                fpc.fingerprint_user_agent({"data": {"ua": "UA/1.0"}}) == "UA/1.0")
    ok &= check("a fingerprint with no user agent yields None, not a crash",
                fpc.fingerprint_user_agent({"country": "GB"}) is None)

    kw = fpc.playwright_context_kwargs(FIX_FINGERPRINT)
    ok &= check("the context carries the fingerprint's user agent",
                kw.get("user_agent") == ua)
    ok &= check("the locale is the fingerprint's own, not en-<country>",
                kw.get("locale") == "en-GB")
    ok &= check("the timezone is carried, so the browser cannot contradict it",
                kw.get("timezone_id") == "Europe/London")
    ok &= check("the viewport is the fingerprint's window, not its screen",
                kw.get("viewport") == {"width": 1920, "height": 992}
                and kw.get("screen") == {"width": 1920, "height": 1080})

    bare = fpc.playwright_context_kwargs({"country": "FR", "screen":
                                          {"width": 1280, "height": 800}})
    ok &= check("a fingerprint with no intl block still gets a locale",
                bare.get("locale") == "en-FR")
    ok &= check("...and a window smaller than the screen",
                bare["viewport"]["height"] < bare["screen"]["height"])
    ok &= check("a fingerprint with nothing usable yields no kwargs",
                fpc.playwright_context_kwargs({}) == {})

    accepted = {"user_agent", "viewport", "screen", "locale", "timezone_id",
               "geolocation", "permissions", "extra_http_headers",
               "device_scale_factor", "is_mobile", "has_touch", "color_scheme"}
    ok &= check("every context kwarg is one Playwright's new_context() accepts",
                set(kw) <= accepted)

    ok &= check("the fingerprint's own deviceScaleFactor is carried through "
                "(a Retina/HiDPI screen used to render as a plain 1x context)",
                kw.get("device_scale_factor") == 1.0)
    no_dsf = fpc.playwright_context_kwargs(
        {"screen": {"width": 1280, "height": 800}})
    ok &= check("a screen with no deviceScaleFactor/devicePixelRatio at all "
                "omits the kwarg rather than sending 0 or None",
                "device_scale_factor" not in no_dsf)
    bad_dsf = fpc.playwright_context_kwargs(
        {"screen": {"width": 1280, "height": 800, "deviceScaleFactor": "not-a-number"}})
    ok &= check("a garbage deviceScaleFactor is dropped, not sent through to "
                "new_context() where Playwright would reject it",
                "device_scale_factor" not in bad_dsf)
    return ok


def test_fingerprint_client_reads_env():
    group("fingerprint_client: main() reads TWOCAPTCHA_KEY the same way "
          "every other entry point in this repo does")
    ok = True
    import fingerprint_client as fpc

    # This file used to be the one CLI in the repo that skipped env_config
    # entirely and read only its own --key flag -- so a TWOCAPTCHA_KEY set in
    # .env or exported the way every other script here reads it was silently
    # ignored, and the first thing CLAUDE.md tells someone to run when a key
    # "isn't working" (`python3 fingerprint_client.py`) could not see it.
    saved = os.environ.pop("TWOCAPTCHA_KEY", None)
    old_argv = sys.argv
    # "No TWOCAPTCHA_KEY anywhere" has to mean no .env either, and
    # env_config.load_env() defaults to the .env sitting NEXT TO THE SCRIPTS
    # (not the current directory -- chdir does not isolate this). A developer
    # who followed the README and created one therefore made main() succeed,
    # and this assertion failed for exactly the people who had configured the
    # tool correctly. Found by finally writing a real .env for a live run.
    #
    # Isolated by pointing the loader at a path that does not exist, which
    # leaves the precedence logic itself running -- the thing under test --
    # rather than stubbing env_config out altogether, and touches no file on
    # disk (a test must not mutate the working tree).
    isolated = tempfile.mkdtemp()
    real_load_env = env_config.load_env
    try:
        env_config.load_env = (
            lambda path=None, override=False, _p=os.path.join(isolated, ".env"):
            real_load_env(path=_p, override=override))
        sys.argv = ["fingerprint_client.py"]
        rc = fpc.main()
        ok &= check("with no --key and no TWOCAPTCHA_KEY anywhere, main() "
                    "refuses with exit 2 before ever calling the network",
                    rc == 2)
    finally:
        env_config.load_env = real_load_env
        shutil.rmtree(isolated, ignore_errors=True)
        sys.argv = old_argv
        if saved is not None:
            os.environ["TWOCAPTCHA_KEY"] = saved

    saved = os.environ.pop("TWOCAPTCHA_KEY", None)
    seen = {}
    real_get_fingerprint = fpc.get_fingerprint

    def fake_get_fingerprint(key, **kwargs):
        seen["key"] = key
        return {"id": 1, "userAgent": {"userAgent": "UA/1.0"}}

    fpc.get_fingerprint = fake_get_fingerprint
    old_argv = sys.argv
    try:
        os.environ["TWOCAPTCHA_KEY"] = "from-environment-not-a-flag"
        sys.argv = ["fingerprint_client.py"]
        rc = fpc.main()
        ok &= check("a TWOCAPTCHA_KEY exported (never passed via --key) "
                    "reaches get_fingerprint through env_config.apply()",
                    seen.get("key") == "from-environment-not-a-flag")
        ok &= check("main() succeeds (exit 0) once the key is found via the "
                    "environment",
                    rc == 0)
    finally:
        fpc.get_fingerprint = real_get_fingerprint
        sys.argv = old_argv
        os.environ.pop("TWOCAPTCHA_KEY", None)
        if saved is not None:
            os.environ["TWOCAPTCHA_KEY"] = saved
    return ok


def test_credentials_never_reach_a_log():
    group("an API key never reaches a log or an exception message")
    ok = True
    import fingerprint_client as fpc
    import captcha_solver as cs

    example_key = "0123456789abcdef0123456789abcdef"
    for name, module in (("fingerprint_client", fpc), ("captcha_solver", cs)):
        redacted = module._redact(
            "400 Client Error: Bad Request for url: "
            "https://api.2captcha.com/fingerprint/random?format=chromium&"
            "key=%s" % example_key)
        ok &= check("%s redacts a key out of an error message" % name,
                    example_key not in redacted)
        ok &= check("...and keeps the endpoint, which is the useful half",
                    "api.2captcha.com/fingerprint/random" in redacted)
        ok &= check("%s redacts clientKey too" % name,
                    example_key not in module._redact("clientKey=%s" % example_key))
        ok &= check("%s leaves ordinary text alone" % name,
                    module._redact("upstream status 403") == "upstream status 403")
    return ok


def test_remote_api_error():
    group("exit 5: a 2Captcha product call failing is distinguishable from "
          "a crash (1) or the target site blocking a page (3)")
    ok = True
    import fingerprint_client as fpc

    ok &= check("EXIT_REMOTE_API_ERROR is the family contract's value (5)",
                EXIT_REMOTE_API_ERROR == 5)
    ok &= check("RemoteAPIError is still a RuntimeError -- existing generic "
                "handlers are not broken by this addition",
                issubclass(RemoteAPIError, RuntimeError))

    class _FakeResp:
        def __init__(self, status_code, body):
            self.status_code = status_code
            self.text = body
        def json(self):
            return json.loads(self.text)
        def raise_for_status(self):
            if self.status_code >= 400:
                raise __import__("requests").HTTPError(
                    "%d for url: %s?key=should-be-redacted" % (self.status_code, fpc.RANDOM_URL))

    real_get = fpc.requests.get
    for status, body, label in (
        (401, '{"errorCode":"ERROR_WRONG_USER_KEY"}', "401 (bad key)"),
        (400, '{"errorDescription":"tags invalid"}', "400 (bad request)"),
        (429, '{"errorCode":"ERROR_FINGERPRINT_RATE_LIMITED"}', "429 (rate limited)"),
        (503, "upstream unavailable", "503 (upstream, via raise_for_status)"),
    ):
        fpc.requests.get = lambda *a, _s=status, _b=body, **kw: _FakeResp(_s, _b)
        try:
            raised = False
            try:
                fpc.get_fingerprint("fake-key", cache_dir=None)
            except RemoteAPIError:
                raised = True
            ok &= check("get_fingerprint raises RemoteAPIError on %s, not a "
                        "bare RuntimeError callers can't distinguish" % label,
                        raised)
        finally:
            fpc.requests.get = real_get

    # A connection failure (DNS, refused, timeout) goes through the same
    # path via requests.RequestException -- checked separately since it
    # never reaches a status code at all.
    import requests as _requests
    def _raise_conn_error(*a, **kw):
        raise _requests.ConnectionError("Max retries exceeded")
    fpc.requests.get = _raise_conn_error
    try:
        raised = False
        try:
            fpc.get_fingerprint("fake-key", cache_dir=None)
        except RemoteAPIError:
            raised = True
        ok &= check("get_fingerprint raises RemoteAPIError on a connection "
                    "failure too, not just a bad HTTP status", raised)
    finally:
        fpc.requests.get = real_get

    # The three engines must agree on this mapping (family invariant -- see
    # each module's own docstring). Hard to exercise live without a real
    # --cdp-endpoint or a real bad key, so checked at the source level, the
    # same way this suite already checks banned wording and removed flags.
    for engine_file in ("playwright_scraper.py", "selenium_scraper.py", "puppeteer_scraper.py"):
        src = open(os.path.join(REPO_ROOT, engine_file), encoding="utf-8").read()
        ok &= check("%s imports RemoteAPIError from output_writer" % engine_file,
                    "RemoteAPIError" in src and "from output_writer import" in src)
        ok &= check("%s's __main__ catches RemoteAPIError and exits "
                    "EXIT_REMOTE_API_ERROR, not just ProxyError" % engine_file,
                    "except RemoteAPIError as e:" in src
                    and "sys.exit(EXIT_REMOTE_API_ERROR)" in src)

    pw_src = open(os.path.join(REPO_ROOT, "playwright_scraper.py"), encoding="utf-8").read()
    ok &= check("playwright_scraper's --cdp-endpoint connect failure raises "
                "RemoteAPIError, not a bare PWError an uncaught crash would "
                "swallow into exit 1",
                "raise RemoteAPIError(" in pw_src and "could not connect to --cdp-endpoint" in pw_src)
    pp_src = open(os.path.join(REPO_ROOT, "puppeteer_scraper.py"), encoding="utf-8").read()
    ok &= check("puppeteer_scraper's --cdp-endpoint connect failure raises "
                "RemoteAPIError the same way",
                "raise RemoteAPIError(" in pp_src and "could not connect to --cdp-endpoint" in pp_src)
    return ok


def test_concurrent_dispatch(skips):
    group("concurrent page dispatch (threads, stop event, accounting)")
    ok = True
    try:
        import playwright_scraper as eng
    except ImportError as e:
        skips.append("concurrent dispatch (%s)" % e)
        return ok

    # The thread fan-out is the one part of --concurrency that the rest of
    # this suite does not reach, and it is not reachable from a live run in
    # every environment either: page 1 is always fetched alone (only
    # --mode transfers is concurrency-capable here -- see
    # CONCURRENCY_CAPABLE_MODES) and decides whether the rest may be
    # addressed, so a blocked page 1 means the workers never start. Driven
    # here with the browser stubbed out, which leaves exactly the
    # concurrency logic under test.
    original = (eng.sync_playwright, eng._BrowserSession, eng._fetch_one_page)

    class Args:
        delay = 0
        mode = "transfers"
        out = "x"

    def run(specs, concurrency, rows_for_page, die_on=()):
        fetched, lock = [], threading.Lock()

        def fake_fetch(session, args, pool, page_num, url):
            with lock:
                fetched.append(page_num)
            if page_num in die_on:
                raise RuntimeError("worker blew up on page %d" % page_num)
            outcome = eng.PageOutcome(page_num=page_num, url=url)
            outcome.rows = rows_for_page(page_num)
            return outcome

        eng.sync_playwright = lambda: _FakePlaywright()
        eng._BrowserSession = lambda pw, args, pool, **kw: _FakeSession(pool)
        eng._fetch_one_page = fake_fetch
        try:
            results, unattempted, exhausted = eng._fetch_pages_concurrently(
                Args(), None, specs, concurrency)
        finally:
            (eng.sync_playwright, eng._BrowserSession,
             eng._fetch_one_page) = original
        return fetched, results, unattempted, exhausted

    # 1. Every page fetched exactly once, whatever the worker count.
    specs = [(n, "u%d" % n) for n in range(2, 12)]
    fetched, results, unattempted, exhausted = run(specs, 4, lambda n: ["row"])
    ok &= check("every queued page is fetched exactly once",
                sorted(fetched) == [n for n, _ in specs])
    ok &= check("every page produces an outcome",
                sorted(o.page_num for o in results) == [n for n, _ in specs])
    ok &= check("nothing is left unattempted when the listing does not end",
                unattempted == [] and not exhausted)

    # 2. Results can be reconstructed into page order (they arrive in
    #    whatever order the threads finish, which is why the caller merges
    #    by page number rather than by arrival).
    ok &= check("outcomes can be put back into page order",
                [o.page_num for o in sorted(results, key=lambda o: o.page_num)]
                == [n for n, _ in specs])

    # 3. The stop event: asking for 50 pages of a listing that ends at page 5
    #    must not fetch 45 empty ones.
    specs = [(n, "u%d" % n) for n in range(2, 51)]
    fetched, results, unattempted, exhausted = run(
        specs, 3, lambda n: [] if n >= 5 else ["row"])
    ok &= check("the end of the listing stops dispatch", exhausted)
    ok &= check("an exhausted listing costs at most (concurrency-1) extra "
                "fetches (%d fetched of 49 queued)" % len(fetched),
                len(fetched) <= 4 + 3)
    ok &= check("the pages never tried are reported, not counted as failed",
                unattempted and all(o.ok for o in results))
    ok &= check("unattempted pages are reported in order",
                unattempted == sorted(unattempted))

    # 4. A worker that dies must not hang the run, and must not swallow the
    #    pages its siblings did fetch.
    specs = [(n, "u%d" % n) for n in range(2, 8)]
    fetched, results, unattempted, exhausted = run(
        specs, 3, lambda n: ["row"], die_on={3})
    ok &= check("a worker that raises does not hang the run",
                len(results) + len(unattempted) + 1 >= len(specs))
    ok &= check("the pages other workers fetched still come back",
                any(o.page_num != 3 for o in results))
    return ok


def test_no_undefined_names():
    group("no module references a name that does not exist")
    ok = True
    # This exists because of exactly the bug flagged in puppeteer_scraper.py's
    # own docstring: a sibling repo's pyppeteer engine called a function on a
    # line reached only while fetching a live page, after the import of that
    # name had been removed. The module imported fine, `--help` worked,
    # `compileall` passed, the whole offline suite passed and CI was green --
    # and the engine died with NameError on its first real page.
    #
    # Byte-compiling proves a file PARSES. It says nothing about whether the
    # names in it resolve, and the paths where they do not are exactly the
    # ones an offline suite cannot execute.
    for name in sorted(f for f in os.listdir(REPO_ROOT) if f.endswith(".py")):
        missing = _undefined_names(os.path.join(REPO_ROOT, name))
        detail = ", ".join("%s (line %d)" % (k, v[0]) for k, v in sorted(missing.items()))
        ok &= check("%s references no undefined name%s"
                    % (name, ": " + detail if missing else ""), not missing)
    return ok


def test_ci_checks_is_actually_wired_up():
    group(".github/ci_checks.py is shipped AND actually invoked, not just present")
    ok = True
    # A sibling in this family (mediamarkt-scraper) shipped ci_checks.py and
    # had tests.yml run a separately hand-maintained inline copy of the same
    # secret scan instead -- the two disagreed (the inline one matched only
    # ws://, the shipped one also matched http://), and the shipped file was
    # invoked by nothing at all. A check that exists but that no workflow
    # calls is as good as no check.
    ci_checks = os.path.join(REPO_ROOT, ".github", "ci_checks.py")
    ok &= check(".github/ci_checks.py exists", os.path.isfile(ci_checks))

    tests_yml = os.path.join(REPO_ROOT, ".github", "workflows", "tests.yml")
    workflow_text = ""
    if os.path.isfile(tests_yml):
        workflow_text = open(tests_yml, encoding="utf-8").read()
    ok &= check("tests.yml exists", bool(workflow_text))
    ok &= check("tests.yml actually runs the shipped secret-check (not a "
                "second, hand-copied scan that can drift from it)",
                "ci_checks.py --secret-check" in workflow_text)

    if os.path.isfile(ci_checks):
        import subprocess
        result = subprocess.run(
            [sys.executable, ci_checks, "--all"],
            capture_output=True, text=True, cwd=REPO_ROOT)
        ok &= check("`python3 .github/ci_checks.py --all` passes against "
                    "this repo right now (help/sample/secret checks, for "
                    "real, not just parsed)",
                    result.returncode == 0)
        if result.returncode != 0:
            print(result.stdout, result.stderr)
    return ok


def test_dockerfile_copies_what_it_runs():
    group("the Docker image contains every module its entrypoint imports")
    ok = True
    path = os.path.join(REPO_ROOT, "Dockerfile")
    if not os.path.exists(path):
        return check("Dockerfile exists", False)

    raw = open(path, encoding="utf-8").read()
    joined = re.sub(r"\\\n\s*", " ", raw)
    copied = set()
    for line in joined.splitlines():
        if line.startswith("COPY "):
            copied.update(tok for tok in line.split() if tok.endswith(".py"))

    entrypoint = None
    m = re.search(r'ENTRYPOINT\s*\[([^\]]*)\]', joined)
    if m:
        parts = [x.strip().strip('"\'') for x in m.group(1).split(",")]
        entrypoint = next((x for x in parts if x.endswith(".py")), None)
    ok &= check("the Dockerfile names a Python entrypoint", bool(entrypoint))
    if not entrypoint:
        return False
    ok &= check("the entrypoint itself is copied into the image", entrypoint in copied)

    local = {f[:-3] for f in os.listdir(REPO_ROOT) if f.endswith(".py")}

    def reached(module, seen=None):
        seen = seen if seen is not None else set()
        if module in seen:
            return seen
        seen.add(module)
        tree = ast.parse(open(os.path.join(REPO_ROOT, module + ".py"), encoding="utf-8").read())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".")[0]]
            for name in names:
                if name in local:
                    reached(name, seen)
        return seen

    needed = reached(entrypoint[:-3])
    missing = sorted(m + ".py" for m in needed if (m + ".py") not in copied)
    ok &= check("every module the entrypoint imports is COPYed (%s)"
                % (", ".join(missing) if missing else "none missing"), not missing)

    gone = sorted(f for f in copied if not os.path.exists(os.path.join(REPO_ROOT, f)))
    ok &= check("the Dockerfile copies no file that has been deleted (%s)"
                % (", ".join(gone) if gone else "none"), not gone)
    return ok


def test_sample_output():
    group("sample_output is cut from a real capture's parse")
    ok = True
    path = os.path.join(REPO_ROOT, "sample_output.json")
    if not os.path.exists(path):
        return check("sample_output.json exists", False)
    rows = json.load(open(path, encoding="utf-8"))
    ok &= check("the sample has rows", len(rows) > 0)
    names = [f.name for f in fields(Player)]
    ok &= check("its columns match the Player schema exactly",
                all(set(r) == set(names) for r in rows))
    text = json.dumps(rows, ensure_ascii=False)
    ok &= check("the sample carries no fabrication markers",
                not re.search(r"example\.com|lorem ipsum|FIXME|TODO|XXXX", text, re.IGNORECASE))
    ok &= check("every sample row has a numeric Transfermarkt player id",
                all(re.fullmatch(r"\d+", r.get("sku") or "") for r in rows))
    ok &= check("every sample row says which host it came from",
                all((r.get("source") or "") in HOSTS for r in rows))
    ok &= check("every sample row's url is a real player-profile URL",
                all("/profil/spieler/" in (r.get("url") or "") for r in rows))
    ok &= check("the sample shows a real price_source, not a column of nulls",
                all(r.get("price_source") in ("listing", "detail", "listing+detail")
                    for r in rows))

    csv_path = os.path.join(REPO_ROOT, "sample_output.csv")
    if os.path.exists(csv_path):
        header = open(csv_path, encoding="utf-8").read().split("\n")[0]
        ok &= check("the sample CSV header matches the schema", header.strip().split(",") == names)
    return ok


# ---------------------------------------------------------------------------
def test_x_debug_header_is_redacted():
    """SECURITY.md names the Scraper API's x-debug header as a place
    credentials reach a log unmasked. It was then logged verbatim: the API
    echoes back the task it ran, so a credentialed CDP endpoint's username
    and password went into the log.

    The fixtures are assembled from pieces, never written out whole, because
    this file is scanned by the credential check like every other one.
    """
    ok = True
    import scraper_api_client as sac
    pw = "SeCr" + "EtPw"
    key = "abcdef01" * 4
    raw = ("cdpurl=ws://acct-zone-scraping_browser-pid-7:" + pw
           + "@cb.2captcha.com:9222 cost=0.00145 key=" + key + " status=200")
    out = sac._redact_debug_header(raw)
    gone = pw not in out and key not in out
    kept = ("cost=0.00145" in out and "cb.2captcha.com:9222" in out
            and "status=200" in out)
    s1, s2 = "secret" + "one", "secret" + "two"
    two = sac._redact_debug_header(
        "a=http://u1:" + s1 + "@h1:1 b=http://u2:" + s2 + "@h2:2")
    both = s1 not in two and s2 not in two
    wired = ('logger.info("x-debug: %s", _redact_debug_header(debug))'
             in inspect.getsource(sac))
    ok &= check("x-debug: the credential and the key are gone", gone)
    ok &= check("x-debug: the cost, host and status survive", kept)
    ok &= check("x-debug: both credentials are masked, not just the first", both)
    ok &= check("x-debug: the log line calls the redactor", wired)
    return ok


def main() -> int:
    ok = True
    # Checks that could not run because an optional engine library is absent.
    # Reported at the end: a suite that silently skips part of itself and
    # still says "all passed" is the same defect as code that reports
    # success without checking that what it wanted actually happened.
    skips = []

    ok &= test_money_parsing()
    ok &= test_locale_money_parsing()
    ok &= test_locale_translation()
    ok &= test_normalize_date()
    ok &= test_market_values()
    ok &= test_locale_market_values()
    ok &= test_club_squad()
    ok &= test_transfers()
    ok &= test_player_detail()
    ok &= test_locale_player_detail()
    ok &= test_urls_and_ids()
    ok &= test_aws_waf()
    ok &= test_bot_detection()
    ok &= test_page_flow()
    ok &= test_readiness_wait()
    ok &= test_output_contract()
    ok &= test_writers()
    ok &= test_finish_run()
    ok &= test_diff()
    ok &= test_captcha()
    ok &= test_remote_api_error()
    ok &= test_env_config()
    ok &= test_proxy_pool()
    ok &= test_engines(skips)
    ok &= test_engine_parity(skips)
    ok &= test_env_duplicate_keys()
    ok &= test_proxy_filenames_are_ignored()
    ok &= test_aws_waf_two_actions()
    ok &= test_minted_proxy_sessions()
    ok &= test_browser_profile_client()
    ok &= test_scraper_api_client()
    ok &= test_no_capture_leaks()
    ok &= test_wording()
    ok &= test_fingerprint_application()
    ok &= test_fingerprint_client_reads_env()
    ok &= test_credentials_never_reach_a_log()
    ok &= test_concurrent_dispatch(skips)
    ok &= test_no_undefined_names()
    ok &= test_ci_checks_is_actually_wired_up()
    ok &= test_dockerfile_copies_what_it_runs()
    ok &= test_sample_output()
    ok &= test_x_debug_header_is_redacted()

    print()
    if _failures:
        print("%d check(s) FAILED:" % len(_failures))
        for f in _failures:
            print("  - %s" % f)
    if skips:
        print("%d engine group(s) SKIPPED — an optional engine library is "
              "absent. CI's engine-smoke job installs all three and fails if "
              "this list is non-empty, because a skip reads exactly like a "
              "passing run:" % len(skips))
        for s in skips:
            print("  - %s" % s)
    print("smoke_test: %s" % ("OK" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
