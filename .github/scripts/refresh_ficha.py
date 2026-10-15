#!/usr/bin/env python3
"""Refresh the profile README character sheet.

Recomputes level/XP from the PUBLIC contribution calendar (exactly what
an anonymous visitor sees on the profile graph) and refreshes quest
dates/statuses from repository metadata. Markers in README.md delimit
the rewritten blocks:

    <!-- ficha:level:ini -->  ...  <!-- ficha:level:fin -->
    <!-- ficha:quests:ini -->  ...  <!-- ficha:quests:fin -->

Rows whose repository cannot be read (HTTP 404, e.g. private repos
without a token) are kept verbatim. The contribution scrape is always
unauthenticated so local runs and CI agree on the same public number.

Exits non-zero when markers are missing or the scraped data looks
implausible, so CI never writes garbage into README.md.
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

USER = "hernanharco"
README = Path(__file__).resolve().parents[2] / "README.md"

LEVEL_INI = "<!-- ficha:level:ini -->"
LEVEL_FIN = "<!-- ficha:level:fin -->"
QUESTS_INI = "<!-- ficha:quests:ini -->"
QUESTS_FIN = "<!-- ficha:quests:fin -->"

MONTHS_ES = ["ene", "feb", "mar", "abr", "may", "jun",
             "jul", "ago", "sep", "oct", "nov", "dic"]
BAR_CELLS = 20
ACTIVE_DAYS = 45  # <= this age counts as "en desarrollo"
TIMEOUT = 30


def http_get(url, token=None, accept="application/json"):
    headers = {"User-Agent": "profile-ficha-refresh", "Accept": accept}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read().decode("utf-8", "replace")


def public_contributions(today):
    """Sum the anonymous contribution calendar for the last 12 months.

    Deliberately unauthenticated: this must equal what a visitor sees.
    """
    frm = today - timedelta(days=365)
    url = (f"https://github.com/users/{USER}/contributions"
           f"?from={frm.isoformat()}&to={today.isoformat()}")
    html = http_get(url, accept="text/html")
    counts = [int(x) for x in re.findall(r"(\d+) contributions? on ", html)]
    empty = len(re.findall(r"No contributions on ", html))
    total = sum(counts)
    # Shape check catches format changes (0 days) and double counting
    # (~2x days): a year of calendar cells must be roughly one year.
    if not 300 <= len(counts) + empty <= 377:
        sys.exit(f"unexpected calendar shape: "
                 f"{len(counts)} active + {empty} empty days")
    if total < 100:
        sys.exit(f"suspicious public contribution total: {total}")
    return total


def repo_info(name, token):
    """Return repo metadata, or None when unreadable (private/deleted)."""
    url = f"https://api.github.com/repos/{USER}/{name}"
    headers = {"User-Agent": "profile-ficha-refresh",
               "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as err:
        if err.code == 404:
            return None
        if err.code in (403, 429):
            sys.exit(f"rate limited fetching {name}: HTTP {err.code}")
        sys.exit(f"HTTP {err.code} fetching {name}")


def month_stamp(today):
    return f"{MONTHS_ES[today.month - 1]} {today.year}"


def level_block(total, today):
    level = total // 100
    pct = total % 100
    remaining = 100 - pct
    filled = round(pct * BAR_CELLS / 100)
    bar = "█" * filled + "░" * (BAR_CELLS - filled)
    xp = f"{total:,}".replace(",", ".")
    return (
        "\n"
        f"      <h3>🆙 Nivel {level} — Guerrero del Código</h3>\n"
        "      <pre>\n"
        f"NIVEL {level}  [{bar}]  {pct}%  ·  {xp} XP\n"
        f"          +{remaining} contribuciones → Nivel {level + 1}\n"
        "      </pre>\n"
        "      <sub>📐 Nivel = contribuciones de los últimos 12 meses "
        f"÷ 100 · actualizado {month_stamp(today)}</sub>\n"
        "      "
    )


def spanish_date(ts):
    dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
    return f"{dt.day} {MONTHS_ES[dt.month - 1]} {dt.year}", dt


def quest_row(line, token, today):
    cells = line.split("|")
    if len(cells) != 6:
        print(f"  ! unexpected row shape, kept: {line[:60]}")
        return line
    quest = cells[1].strip()
    match = (re.search(rf"github\.com/{USER}/([^)\s]+)", quest)
             or re.search(r"🔒\s*(\S+)", quest))
    if not match:
        print(f"  ! no repo name in row, kept: {quest[:60]}")
        return line
    info = repo_info(match.group(1), token)
    if info is None:
        # Private or renamed: preserve the row as-is instead of guessing.
        print(f"  - unreadable, kept: {quest[:60]}")
        return line
    date_str, pushed = spanish_date(info["pushed_at"])
    language = info.get("language") or "—"
    age = (today - pushed.date()).days
    status = "🟢 En desarrollo" if age <= ACTIVE_DAYS else "🟡 En pausa"
    return f"| {quest} | {language} | {date_str} | {status} |"


def quests_block(body, token, today):
    # Old header/separator lines in the body are dropped: table
    # rebuilds them below, so keeping them would duplicate the header.
    rows = []
    for line in body.split("\n"):
        if not line.startswith("|"):
            continue
        if line.startswith("| Quest") or line.startswith("|:------"):
            continue
        rows.append(quest_row(line, token, today))
    table = "\n".join([
        "| Quest | Lenguaje | Última actividad | Estado |",
        "|:------|:--------:|:-----------------:|:------:|",
        *rows,
    ])
    return "\n\n" + table + "\n\n"


def replace_block(text, ini, fin, body):
    if text.count(ini) != 1 or text.count(fin) != 1:
        sys.exit(f"marker missing or duplicated: {ini} / {fin}")
    start = text.index(ini) + len(ini)
    end = text.index(fin, start)
    return text[:start] + body + text[end:]


def main():
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    today = datetime.now(timezone.utc).date()

    text = README.read_text(encoding="utf-8")
    total = public_contributions(today)
    text = replace_block(text, LEVEL_INI, LEVEL_FIN, level_block(total, today))

    start = text.index(QUESTS_INI) + len(QUESTS_INI)
    end = text.index(QUESTS_FIN, start)
    text = replace_block(text, QUESTS_INI, QUESTS_FIN,
                         quests_block(text[start:end], token, today))

    README.write_text(text, encoding="utf-8")
    print(f"OK: {total} public contributions -> level {total // 100}")


if __name__ == "__main__":
    main()
