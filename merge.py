#!/usr/bin/env python3
"""
Fuehrt zwei Dienstplan-iCal-Feeds (Rettungsdienst/SIEDA + Luftrettung/Fleetplan)
zu einem gefilterten, umbenannten Familien-Kalender zusammen.

Quelle 1 (DRK Rettungsdienst, SIEDA):
  - "Urlaub", "Krank" und "Diensttausch" werden entfernt
  - Tagdienst-Codes (T..) -> "DRK Tag", Nachtdienst-Codes (N..) -> "DRK Nacht"

Quelle 2 (Luftrettung, Fleetplan):
  - "Ortstag"/"Ortstag plus" (DR-04/DR-05) und "Krank" (ZZ-4) werden entfernt
  - Flugdienste "DR-.. Tag/Nacht .." -> "DRF Dienst Tag"/"DRF Dienst Nacht",
    an direkt aufeinanderfolgenden Kalendertagen zu einem Block zusammengefasst
  - "Urlaub" (ZZ-12) bleibt, direkt angrenzende Urlaubsabschnitte werden zu
    einem Block zusammengefasst ("Urlaub" / "Urlaub (N Tage)")
  - "Elternzeit" (ZZ-19) bleibt, taegliche Einzel-Marker werden zu einem
    ganztaegigen Block zusammengefasst ("Elternzeit" / "Elternzeit (N Tage)")
  - alle uebrigen Eintraege (Positionierung, Reise, Schulung, Simulator,
    Buero, Besprechung, Stationsarbeiten) bleiben inhaltlich unveraendert,
    nur das fuehrende Code-Praefix (z.B. "FD-13 ") wird entfernt

Ausgabe: dienstplan.ics im selben Ordner wie dieses Skript.
"""
import re
import uuid
from datetime import datetime, timedelta, timezone

import requests

FEED1_URL = "https://careman.itc.drk.de:8082/calendar/roster/cf08d2e5-81ed-40a4-9a5e-8844f2982fea.ics"
FEED2_URL = "https://drf.fleetplan.net/api/ical/feed/5067/5077085?token=41191133-31d5-4efc-aed4-943b26a45115"
OUT_FILE = "dienstplan.ics"

PROP_RE_TMPL = r"(?m)^{name}(;[^:\r\n]*)?:(.*)$"


def unfold(text: str) -> str:
    text = text.replace("\r\n", "\n")
    return re.sub(r"\n[ \t]", "", text)


def get_events(ics_text: str):
    return re.findall(r"BEGIN:VEVENT.*?END:VEVENT", ics_text, re.S)


def prop_line(block: str, name: str):
    m = re.search(PROP_RE_TMPL.format(name=name), block)
    return m.group(0).rstrip() if m else None


def prop_value(block: str, name: str):
    m = re.search(PROP_RE_TMPL.format(name=name), block)
    return m.group(2).strip() if m else None


def date_key(dt_value: str):
    m = re.match(r"^(\d{8})", dt_value or "")
    return m.group(1) if m else None


def add_days(yyyymmdd: str, days: int) -> str:
    d = datetime.strptime(yyyymmdd, "%Y%m%d") + timedelta(days=days)
    return d.strftime("%Y%m%d")


def format_vevent(uid, dtstart_line, dtend_line, summary, loc_line, dtstamp):
    lines = ["BEGIN:VEVENT", f"UID:{uid}", dtstart_line, dtend_line, f"DTSTAMP:{dtstamp}", f"SUMMARY:{summary}"]
    if loc_line:
        lines.append(loc_line)
    lines.append("END:VEVENT")
    return "\r\n".join(lines)


def main():
    now_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_events = []

    # ---------- Quelle 1: DRK Rettungsdienst ----------
    raw1 = unfold(requests.get(FEED1_URL, timeout=30).text)
    for ev in get_events(raw1):
        summary = prop_value(ev, "SUMMARY")
        if summary is None:
            continue
        if re.search("Urlaub", summary) or re.search("Krank", summary) or re.search("Diensttausch", summary):
            continue

        new_summary = summary
        if re.match(r"^T\d+", summary):
            new_summary = "DRK Tag"
        elif re.match(r"^N\d+", summary):
            new_summary = "DRK Nacht"

        out_events.append(format_vevent(
            f"drk-{uuid.uuid4()}", prop_line(ev, "DTSTART"), prop_line(ev, "DTEND"),
            new_summary, prop_line(ev, "LOCATION"), now_stamp,
        ))

    # ---------- Quelle 2: Luftrettung (Fleetplan) ----------
    raw2 = unfold(requests.get(FEED2_URL, timeout=30).text)

    duty_events = []
    urlaub_events = []
    elternzeit_events = []
    passthrough_events = []

    for ev in get_events(raw2):
        summary = prop_value(ev, "SUMMARY")
        if summary is None:
            continue
        if re.search("Ortstag", summary) or re.search("Krank", summary):
            continue

        dtstart_line = prop_line(ev, "DTSTART")
        dtend_line = prop_line(ev, "DTEND")
        dtstart_val = prop_value(ev, "DTSTART")
        dtend_val = prop_value(ev, "DTEND")
        loc_line = prop_line(ev, "LOCATION")

        if re.match(r"^DR-\d+\s+Tag\b", summary):
            duty_events.append({"category": "Tag", "dtstart_line": dtstart_line, "dtend_line": dtend_line,
                                 "dtstart_val": dtstart_val, "date_key": date_key(dtstart_val)})
        elif re.match(r"^DR-\d+\s+Nacht\b", summary):
            duty_events.append({"category": "Nacht", "dtstart_line": dtstart_line, "dtend_line": dtend_line,
                                 "dtstart_val": dtstart_val, "date_key": date_key(dtstart_val)})
        elif re.search("Urlaub", summary):
            urlaub_events.append({"start_key": date_key(dtstart_val), "end_key": date_key(dtend_val), "loc_line": loc_line})
        elif re.search("Elternzeit", summary):
            elternzeit_events.append({"start_key": date_key(dtstart_val), "loc_line": loc_line})
        else:
            clean_summary = re.sub(r"^(?:FD|PUL|DR|ST|ZZ)-\d+\s*", "", summary)
            passthrough_events.append(format_vevent(
                f"drf-{uuid.uuid4()}", dtstart_line, dtend_line, clean_summary, loc_line, now_stamp,
            ))

    # --- Flugdienste (Tag/Nacht) zu Bloecken zusammenfassen ---
    duty_sorted = sorted(duty_events, key=lambda e: e["dtstart_val"])
    i = 0
    while i < len(duty_sorted):
        block_end = i
        while (block_end + 1 < len(duty_sorted)
               and duty_sorted[block_end + 1]["category"] == duty_sorted[i]["category"]
               and duty_sorted[block_end + 1]["date_key"] == add_days(duty_sorted[block_end]["date_key"], 1)):
            block_end += 1
        length = block_end - i + 1
        first, last = duty_sorted[i], duty_sorted[block_end]
        title = f"DRF Dienst {first['category']} ({length} Tage)" if length >= 2 else f"DRF Dienst {first['category']}"
        out_events.append(format_vevent(f"drf-block-{uuid.uuid4()}", first["dtstart_line"], last["dtend_line"], title, None, now_stamp))
        i = block_end + 1

    # --- Urlaub (ganztaegig) zusammenfassen ---
    urlaub_sorted = sorted(urlaub_events, key=lambda e: e["start_key"])
    i = 0
    while i < len(urlaub_sorted):
        block_end = i
        while (block_end + 1 < len(urlaub_sorted)
               and urlaub_sorted[block_end + 1]["start_key"] == urlaub_sorted[block_end]["end_key"]):
            block_end += 1
        first, last = urlaub_sorted[i], urlaub_sorted[block_end]
        days = (datetime.strptime(last["end_key"], "%Y%m%d") - datetime.strptime(first["start_key"], "%Y%m%d")).days
        title = f"Urlaub ({days} Tage)" if days >= 2 else "Urlaub"
        dtstart = f"DTSTART;VALUE=DATE:{first['start_key']}"
        dtend = f"DTEND;VALUE=DATE:{last['end_key']}"
        out_events.append(format_vevent(f"drf-urlaub-{uuid.uuid4()}", dtstart, dtend, title, first["loc_line"], now_stamp))
        i = block_end + 1

    # --- Elternzeit (taegliche Marker) zusammenfassen ---
    ez_sorted = sorted(elternzeit_events, key=lambda e: e["start_key"])
    i = 0
    while i < len(ez_sorted):
        block_end = i
        while (block_end + 1 < len(ez_sorted)
               and ez_sorted[block_end + 1]["start_key"] == add_days(ez_sorted[block_end]["start_key"], 1)):
            block_end += 1
        length = block_end - i + 1
        first = ez_sorted[i]
        title = f"Elternzeit ({length} Tage)" if length >= 2 else "Elternzeit"
        dtstart = f"DTSTART;VALUE=DATE:{first['start_key']}"
        dtend = f"DTEND;VALUE=DATE:{add_days(ez_sorted[block_end]['start_key'], 1)}"
        out_events.append(format_vevent(f"drf-elternzeit-{uuid.uuid4()}", dtstart, dtend, title, first["loc_line"], now_stamp))
        i = block_end + 1

    out_events.extend(passthrough_events)

    header = "\r\n".join([
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Familie//Dienstplan-Merge//DE",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Dienstplan (DRK + DRF)",
    ])
    footer = "END:VCALENDAR"
    body = "\r\n".join(out_events)
    full_ics = f"{header}\r\n{body}\r\n{footer}\r\n"

    with open(OUT_FILE, "w", encoding="utf-8", newline="") as f:
        f.write(full_ics)

    print(f"Fertig: {len(out_events)} Ereignisse geschrieben nach {OUT_FILE}")


if __name__ == "__main__":
    main()
