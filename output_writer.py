"""
output_writer.py
-----------------
Shared row models + JSON/CSV writers used by all three scrapers.

Two kinds of row, not one
-------------------------
Every other member of this scraper family reads ONE kind of thing (a
product, however it is described). Transfermarkt is not a catalogue: a
listing or a profile page describes a PERSON (or a club), and a transfer
record describes an EVENT between two clubs. Folding both into one dataclass
would mean a "from_club"/"to_club" pair sitting permanently null on every
player row and a "position"/"market value" pair sitting permanently null on
every transfer row — which is the exact column CLAUDE.md's own family rule
says should not exist ("a column that is null on every row of every run
should not exist"). So there are two row classes here, the way
amazon-scraper added a second (`Review`) for the one mode that is not a
product either:

    Player     --mode market-values, club-squad, player
    Transfer   --mode transfers

Both still open on the SAME short family prefix — `source`, `scraped_at`,
`url`, `sku`, `title`, `image_url`, `price`, `currency`, `category` — so a
consumer already written against another repo in this family reads the
first nine columns unchanged. `price_source` is Player-only: it records
which page a market value was confirmed against, which has no equivalent
for a transfer fee (there is nothing to cross-check a fee reading against),
and appending it as a permanently-null tenth column on Transfer would be
exactly the same mistake this paragraph opened with.

`sku` is reused for whichever id is actually unique for the mode, per the
family rule ("dedupe on whatever is actually unique"): a player's
Transfermarkt id for Player rows, a transfer's own id for Transfer rows — a
player can transfer many times, so the player id is NOT unique on a
Transfer run.

`price` really is a price here, not a stretch: a market value is what a
`price`/`currency` pair was designed to hold, and a transfer fee even more
directly so. That is also why diff_runs.py's price-monitoring model
(added/removed/changed by sku) carries over to this repo almost unedited —
see that file's docstring.
"""

import csv
import json
from dataclasses import dataclass, asdict, field, fields
from datetime import datetime, timezone
from typing import Optional, List, Set, Sequence, Any, Type


# Every row in this repo comes from transfermarkt.com — there is no
# multi-hostname platform to distinguish here the way MediaMarkt's ten
# country sites or Amazon's twenty-one marketplaces need `source` to tell
# apart. Kept anyway, as the family's own provenance column and because a
# consumer merging output from several repos in this family expects it.
SOURCE_DEFAULT = "transfermarkt.com"


@dataclass
class Player:
    source: str = SOURCE_DEFAULT
    scraped_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    url: str = ""
    sku: Optional[str] = None          # Transfermarkt's numeric player id
    title: Optional[str] = None        # display name, as printed in a listing row
    image_url: Optional[str] = None    # portrait thumbnail
    # Market value in EUR. Transfermarkt prices every market value in euros
    # regardless of which locale or currency-displaying site served the page
    # (checked on the English-language .com site, which still prints "€"),
    # so this is EUR whenever it is not None — there is no multi-currency
    # case to guess at here, unlike this family's e-commerce members.
    price: Optional[float] = None
    currency: Optional[str] = None
    # Where `price` was read from:
    #   "listing"        — the market-values or club-squad table's own price
    #                       cell, with no detail page fetched to check it.
    #   "detail"         — a --mode player run, reading the profile header's
    #                       market-value widget directly.
    #   "listing+detail" — a listing row whose player id was ALSO fetched as
    #                       a detail page in the same run, and the two agreed.
    # There is no "confirmed against a rendered tile" case here the way the
    # e-commerce members of this family have one: Transfermarkt publishes no
    # structured data (JSON-LD) for either page kind (checked on a ranking
    # page, a squad page and a player profile — zero <script
    # type="application/ld+json"> blocks on any of them), so both readings
    # are DOM readings and neither is more authoritative than the other.
    price_source: Optional[str] = None
    category: Optional[str] = None     # which listing this came from

    # ---- Transfermarkt-specific, appended so the family prefix above is stable ----
    rank: Optional[int] = None         # market-values only; the site's own numbered rank (ties share one)
    page: Optional[int] = None         # which listing page this row came from (1-based)
    row_index: Optional[int] = None    # position within that page, as the site ordered it (0-based)
    position: Optional[str] = None     # football position, e.g. "Centre-Forward"
    shirt_number: Optional[int] = None
    age: Optional[int] = None
    nationality: Optional[str] = None       # primary (first flag on the row/header)
    nationalities: Optional[List[str]] = None  # every flag shown — some players hold two
    club: Optional[str] = None
    club_id: Optional[str] = None

    # ---- populated by --mode player (a detail page) only; null on a listing run ----
    # The site's "Full name" (or, on a profile that labels it "Name in home
    # country" instead — measured on Haaland's — that value) from the
    # profile's separate info-table panel; None when neither row is present,
    # never a copy of `title` (the display name). See
    # product_parser._info_table_map.
    full_name: Optional[str] = None
    birth_date: Optional[str] = None
    birth_place: Optional[str] = None
    height_m: Optional[float] = None
    agent: Optional[str] = None
    league: Optional[str] = None
    league_id: Optional[str] = None
    joined_date: Optional[str] = None
    contract_until: Optional[str] = None
    market_value_last_update: Optional[str] = None
    caps: Optional[int] = None
    international_goals: Optional[int] = None


@dataclass
class Transfer:
    """One transfer record from the latest-transfers listing.

    Deliberately NOT a Player: a transfer is an event between two clubs, and
    the same player can appear in many of these across a season, which is
    exactly why `sku` here is the transfer's own id and not the player's —
    see the module docstring.
    """
    source: str = SOURCE_DEFAULT
    scraped_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    url: str = ""
    sku: Optional[str] = None          # Transfermarkt's transfer id
    title: Optional[str] = None        # player name
    image_url: Optional[str] = None
    price: Optional[float] = None      # transfer fee, EUR — None when not disclosed
    currency: Optional[str] = None
    category: Optional[str] = None     # "latest-transfers"

    # ---- Transfermarkt-specific ----
    page: Optional[int] = None
    row_index: Optional[int] = None
    player_id: Optional[str] = None
    position: Optional[str] = None
    age: Optional[int] = None
    nationality: Optional[str] = None
    nationalities: Optional[List[str]] = None
    from_club: Optional[str] = None
    from_club_id: Optional[str] = None
    from_league: Optional[str] = None
    to_club: Optional[str] = None
    to_club_id: Optional[str] = None
    to_league: Optional[str] = None
    # "disclosed"   — price holds a real euro figure
    # "loan"        — a loan move; price is None (loan fees are not printed here)
    # "free"        — a free transfer; price is None
    # "undisclosed" — the site printed "?": a fee exists but was not published
    # "unknown"     — the fee cell could not be read at all (a markup change)
    fee_type: Optional[str] = None


# Row class by --mode, so an engine maps its mode to a schema in one place.
ROW_CLASS_BY_MODE = {
    "market-values": Player,
    "club-squad": Player,
    "player": Player,
    "transfers": Transfer,
}

# Modes whose rows are one-per-sku, and therefore safe to dedupe on `sku` and
# to hand to diff_runs.py. All four qualify: a market-values/club-squad/
# player run is one row per player id, and a transfers run is one row per
# transfer id (see the module docstring for why that is a DIFFERENT kind of
# id from the other three modes, and why that is still fine here — diff_runs
# only cares that `sku` is unique within one run, not what it represents).
UNIQUE_BY_SKU_MODES = ("market-values", "club-squad", "player", "transfers")


def dedupe_by_key(rows: Sequence[Any], seen: Set[str], key: str = "sku") -> List[Any]:
    """Drop rows whose key already appeared earlier in this same run.

    `seen` is mutated in place, so callers thread the same set across pages —
    a stale or repeating next-page link then re-parses a page without
    duplicating its rows into the final output.

    A row with no key is always kept: there is nothing to check a duplicate
    against, and dropping it would be a silent data loss rather than a
    duplicate removal.
    """
    fresh = []
    for r in rows:
        val = getattr(r, key, None)
        if val is None or val not in seen:
            if val is not None:
                seen.add(val)
            fresh.append(r)
    return fresh


def dedupe_by_sku(rows: Sequence[Any], seen: Set[str]) -> List[Any]:
    return dedupe_by_key(rows, seen, key="sku")


# CSV cannot hold a list (nationalities). Joining with " | " keeps the cell
# readable in a spreadsheet and round-trippable by splitting on the same
# separator; the JSON output keeps the real list.
LIST_CSV_SEPARATOR = " | "


def _csv_value(v: Any) -> Any:
    if isinstance(v, (list, tuple)):
        return LIST_CSV_SEPARATOR.join(str(x) for x in v)
    return v


def write_json(rows: Sequence[Any], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump([asdict(r) for r in rows], f, ensure_ascii=False, indent=2)


def write_csv(rows: Sequence[Any], path: str, row_cls: Type = Player) -> None:
    # An empty result still gets the header row, from `row_cls` rather than
    # the first row, so a mode that finds nothing still writes the columns
    # that mode would have used.
    fieldnames = [f.name for f in fields(row_cls)]
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: _csv_value(v) for k, v in asdict(r).items()})


# Exit code used when a run completes but produced nothing.
EXIT_NO_PRODUCTS = 4

# Exit code for a run blocked by a bot-check/challenge page before parsing
# even started.
EXIT_BLOCKED = 3

# Exit code for a run that gathered SOME rows and then stopped early.
EXIT_PARTIAL = 6

# Exit code for a failure in one of THIS PROJECT's own 2Captcha-product calls
# -- the Fingerprint API rejecting a request (bad key, bad --tags, rate
# limit), or a Scraping Browser CDP connection failing (e.g. profile_locked)
# -- as opposed to EXIT_BLOCKED (the TARGET SITE refusing a page) or an
# uncaught crash (1). Per CLAUDE.md's family exit-code contract ("5" =
# "remote API error"). Deliberately NOT what a captcha-solve failure gets:
# per that same document's captcha section, a solver error is a WARNING that
# lets the run continue (see captcha_solver.py and each engine's
# handle_captcha_if_present) -- exit 5 is for calls the user explicitly
# opted into (--fingerprint, --cdp-endpoint) where silently continuing
# without them would hide a billing/plan/profile-lock problem rather than a
# page the site declined to serve.
EXIT_REMOTE_API_ERROR = 5


class RemoteAPIError(RuntimeError):
    """A 2Captcha product call (Fingerprint API, Scraping Browser CDP
    connect) failed on its own terms, not the target site blocking a page.

    Raised by fingerprint_client.get_fingerprint and by each engine's
    --cdp-endpoint connect path; caught once at each engine's entry point
    and mapped to EXIT_REMOTE_API_ERROR, so the three engines cannot drift
    on which of 1 (crash) / 3 (blocked) / 5 (remote API error) a given
    failure gets -- before this, both paths raised a bare RuntimeError with
    no engine catching it, so either failure reached the interpreter as an
    unhandled exception and exited 1 (a raw traceback, no run-metadata
    sidecar) regardless of which one it actually was.
    """


def write_run_meta(out_prefix: str, meta: dict) -> str:
    """Write a run-metadata sidecar next to the output, return its path."""
    path = f"{out_prefix}.meta.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"[+] Wrote run metadata -> {path} (status={meta.get('status')})")
    return path


def run_meta(status: str, stop_reason: str, pages_requested: int,
             pages_completed: int, start_url: str, final_url: str,
             products: int, pages_failed: Optional[List[int]] = None,
             mode: str = "market-values", source: str = SOURCE_DEFAULT) -> dict:
    """Build the metadata dict for a finished run. See mediamarkt-scraper's
    output_writer.py for the full rationale; unchanged here."""
    return {
        "source": source,
        "mode": mode,
        "status": status,
        "stop_reason": stop_reason,
        "pages_requested": pages_requested,
        "pages_completed": pages_completed,
        "pages_failed": pages_failed or [],
        "products": products,
        "start_url": start_url,
        "final_url": final_url,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }


def save(rows: Sequence[Any], out_prefix: str, fmt: str,
         allow_empty: bool = False, row_cls: Type = Player) -> int:
    """Write JSON/CSV and return a process exit code.

    On zero rows, nothing is written at all unless `allow_empty` — see the
    family invariant in CLAUDE.md §8: a run that finds nothing must not
    silently replace yesterday's good output with an empty file.
    """
    if not rows and not allow_empty:
        print(f"[!] 0 rows — refusing to write {out_prefix}.json/.csv, so an "
              f"earlier good result isn't overwritten with an empty one. "
              f"Pass --allow-empty if an empty result is the expected answer.")
        return EXIT_NO_PRODUCTS

    if fmt in ("json", "both"):
        write_json(rows, f"{out_prefix}.json")
        print(f"[+] Saved {len(rows)} row(s) -> {out_prefix}.json")
    if fmt in ("csv", "both"):
        write_csv(rows, f"{out_prefix}.csv", row_cls=row_cls)
        print(f"[+] Saved {len(rows)} row(s) -> {out_prefix}.csv")
    return 0 if rows else EXIT_NO_PRODUCTS


# Stop reasons that mean the run saw everything there was to see.
COMPLETE_STOP_REASONS = ("completed", "pagination_exhausted", "no_new_products",
                         "single_page_mode")


def finish_run(rows: Sequence[Any], out_prefix: str, fmt: str,
               allow_empty: bool, *, blocked: bool, stop_reason: str,
               pages_requested: int, pages_completed: int,
               start_url: str, final_url: str,
               pages_failed: Optional[List[int]] = None,
               mode: str = "market-values", source: str = SOURCE_DEFAULT) -> int:
    """Write output + the run-metadata sidecar; return the exit code.

    Shared by all three browser engines so the status/exit-code mapping
    cannot drift between them. See mediamarkt-scraper's output_writer.py for
    the full rationale; the logic here is unchanged.
    """
    complete = stop_reason in COMPLETE_STOP_REASONS
    row_cls = ROW_CLASS_BY_MODE.get(mode, Player)
    rc = save(rows, out_prefix, fmt, allow_empty=allow_empty, row_cls=row_cls)
    wrote_output = bool(rows) or allow_empty

    if wrote_output:
        status = "complete" if (rows and complete) else (
            "partial" if rows else "failed")
        write_run_meta(out_prefix, run_meta(
            status=status, stop_reason=stop_reason,
            pages_requested=pages_requested, pages_completed=pages_completed,
            pages_failed=pages_failed, mode=mode, source=source,
            start_url=start_url, final_url=final_url, products=len(rows)))

    if not rows:
        return EXIT_BLOCKED if blocked else rc
    if not complete:
        print(f"[!] Partial run: stopped after {pages_completed} of "
              f"{pages_requested} page(s) ({stop_reason}). The output holds "
              f"what was gathered, but it is NOT a complete view — see "
              f"{out_prefix}.meta.json.")
        return EXIT_PARTIAL
    return rc
