#!/usr/bin/env python3
"""
testudo_seat_watcher.py

Polls UMD's Testudo Schedule of Classes for CMSC216, CMSC250, and STAT410
(Fall 2026, term 202608) and sends a push notification to your phone
whenever open seats appear in a section that had none before, telling you
the course, section number, and meeting time.

Notifications go through ntfy.sh (free, no signup required):
  1. Install the "ntfy" app on your phone (iOS App Store / Google Play).
  2. In the app, subscribe to a topic name of your choosing -- pick
     something private/hard-to-guess, since anyone who knows your topic
     name can see your notifications (it's not a secret channel, just an
     unlisted one). e.g. "gavin-umd-seats-8f3k2"
  3. Run this script with that topic name (see USAGE below).

USAGE
    # run continuously (default 5 min polling); you'll be prompted to pick
    # which sections to watch for each of CMSC216, CMSC250, and STAT410
    python testudo_seat_watcher.py --ghipszerCMNS gavin-umd-seats-8f3k2

    # check every 2 minutes instead
    python testudo_seat_watcher.py --topic gavin-umd-seats-8f3k2 --interval 120

    # skip the prompt by specifying sections up front --
    # format is "COURSE=sec1,sec2 COURSE=sec1" (space-separated per course,
    # comma-separated section IDs). Any course left out watches ALL sections.
    python testudo_seat_watcher.py --topic gavin-umd-seats-8f3k2 \
        --sections "CMSC216=0101,0102 STAT410=0201"

    # one-off check for cron / Task Scheduler (no prompt, watches everything
    # unless --sections is also given)
    python testudo_seat_watcher.py --topic gavin-umd-seats-8f3k2 --once
    python testudo_seat_watcher.py --topic gavin-umd-seats-8f3k2 --once --sections "CMSC216=0101,0102"

DEPENDENCIES
    pip install requests beautifulsoup4

NOTES
    - This only reads public Testudo pages (the same ones you'd see in a
      browser) -- no login, no scraping of anything non-public.
    - State (which sections had how many open seats last check) is saved
      to testudo_seat_state.json next to this script, so you won't get
      re-notified for a seat count that hasn't changed between runs.
    - Testudo's HTML occasionally changes; if this stops finding sections,
      the CSS selectors in fetch_sections() below are the first place to
      check (open the section-list URL in a browser and inspect it).
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from datetime import datetime

import requests
from bs4 import BeautifulSoup

TERM = "202701"  # Spring 2027 (UMD term codes: YYYY + "08" = Fall, "01" = Spring)
COURSES = []
BASE_URL = "https://app.testudo.umd.edu/soc"
STATE_FILE = Path(__file__).with_name("testudo_seat_state.json")
USER_AGENT = "Mozilla/5.0 (compatible; SeatWatcher/1.0; personal, non-commercial use)"


def fetch_sections(course_id: str, term: str = TERM) -> list[dict]:
    """Fetch and parse all sections for a course from Testudo."""
    url = f"{BASE_URL}/{term}/sections"
    resp = requests.get(
        url,
        params={"courseIds": course_id},
        headers={"User-Agent": USER_AGENT},
        timeout=15,
    )
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    def to_int(s: str) -> int:
        try:
            return int(s.strip().rstrip(","))
        except (ValueError, AttributeError):
            return 0

    sections = []
    for div in soup.select(".section"):
        def text(sel: str) -> str:
            el = div.select_one(sel)
            return el.get_text(strip=True) if el else ""

        section_id = text(".section-id")
        if not section_id:
            continue

        instructors = [
            e.get_text(strip=True)
            for e in div.select(".section-instructor")
            if e.get_text(strip=True)
        ]

        sections.append(
            {
                "course": course_id,
                "section": section_id,
                "instructors": instructors,
                "total_seats": to_int(text(".total-seats-count")),
                "open_seats": to_int(text(".open-seats-count")),
                "waitlist": to_int(text(".waitlist-count")),
                "days": text(".section-days"),
                "start": text(".class-start-time").strip(" -"),
                "end": text(".class-end-time"),
                "building": text(".building-code"),
                "room": text(".class-room"),
            }
        )
    return sections


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def send_notification(topic: str, title: str, message: str) -> None:
    """Send a push notification to your phone via ntfy.sh."""
    try:
        requests.post(
            f"https://ntfy.sh/{topic}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": "high", "Tags": "bell"},
            timeout=10,
        )
    except requests.RequestException as e:
        print(f"[warn] failed to send notification: {e}")


def prompt_for_sections() -> dict[str, set[str]]:
    """Interactively ask which sections to watch for each course.

    Pressing Enter with no input for a course means "watch all sections."
    """
    print("Choose which sections to watch for each class.")
    print("(Enter section numbers separated by commas, e.g. 0101,0102 -- or just press Enter to watch ALL sections of that class.)\n")

    section_filter: dict[str, set[str]] = {}
    for course in COURSES:
        raw = input(f"  {course} sections to watch [all]: ").strip()
        if raw:
            section_filter[course] = {s.strip() for s in raw.split(",") if s.strip()}
    print()
    return section_filter


def parse_section_filter(raw: str | None) -> dict[str, set[str]]:
    """Parse --sections "CMSC216=0101,0102 STAT410=0201" into a filter dict.

    A course with no entry in the returned dict means "watch all sections"
    (the check_once loop treats an absent key that way).
    """
    filt: dict[str, set[str]] = {}
    if not raw:
        return filt
    for chunk in raw.split():
        if "=" not in chunk:
            print(f"[warn] ignoring malformed --sections entry: '{chunk}' (expected COURSE=sec1,sec2)")
            continue
        course, secs = chunk.split("=", 1)
        course = course.strip().upper()
        section_ids = {s.strip() for s in secs.split(",") if s.strip()}
        filt[course] = section_ids
        if course not in COURSES:
            COURSES.append(course)
    return filt


def check_once(
    topic: str,
    state: dict,
    section_filter: dict[str, set[str]] | None = None,
    quiet: bool = False,
) -> dict:
    """Check all watched courses once; notify on newly opened seats.

    section_filter, if given, maps course -> set of section IDs to watch.
    A course absent from section_filter (or an empty/None filter overall)
    means every section of that course is watched.
    """
    section_filter = section_filter or {}

    for course in COURSES:
        try:
            sections = fetch_sections(course)
        except requests.RequestException as e:
            print(f"[warn] failed to fetch {course}: {e}")
            continue

        allowed = section_filter.get(course)  # None = watch all sections
        if allowed is not None:
            sections = [s for s in sections if s["section"] in allowed]

        if not quiet:
            print(f"  {course}: {len(sections)} section(s) being watched")

        prev = state.get(course, {})
        current = {}

        for sec in sections:
            key = sec["section"]
            current[key] = sec["open_seats"]

            prev_open = prev.get(key, 0)
            if not quiet:
                print(f"    {course} {key} ({', '.join(sec['instructors']) or 'TBA'}): {sec['open_seats']} open / {sec['total_seats']}, waitlist {sec['waitlist']}")
            now_open = sec["open_seats"]

            if now_open > 0 and now_open > prev_open:
                when = f'{sec["days"]} {sec["start"]}-{sec["end"]}'.strip()
                where = f'{sec["building"]} {sec["room"]}'.strip()
                instr = ", ".join(sec["instructors"]) or "TBA"
                msg = (
                    f'{course} section {key}: {now_open} seat(s) open\n'
                    f'{when} in {where}\n'
                    f'Instructor: {instr}'
                )
                print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}")
                send_notification(topic, f"Seat open: {course} {key}", msg)

        state[course] = current

    return state


def main():
    parser = argparse.ArgumentParser(description="Watch Testudo for newly opened seats.")
    parser.add_argument(
        "--topic",
        required=True,
        help="ntfy.sh topic name to notify (pick something private/unguessable)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        help="seconds between checks when looping (default: 60 = 1 min)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="check once and exit instead of looping (good for cron / Task Scheduler)",
    )
    parser.add_argument(
        "--sections",
        default=None,
        help=(
            'restrict which sections are watched, e.g. '
            '"CMSC216=0101,0102 STAT410=0201". '
            "Any course left out watches all of its sections. "
            "If omitted (and --once isn't set), you'll be prompted interactively instead."
        ),
    )
    args = parser.parse_args()

    state = load_state()

    if args.sections:
        section_filter = parse_section_filter(args.sections)
    elif args.once:
        section_filter = {}  # non-interactive context (e.g. cron) -> watch everything
    else:
        section_filter = prompt_for_sections()

    if section_filter:
        print("Section filter in effect:")
        for course in COURSES:
            watching = ", ".join(sorted(section_filter[course])) if course in section_filter else "ALL sections"
            print(f"  {course}: {watching}")

    if args.once:
        state = check_once(args.topic, state, section_filter)
        save_state(state)
        return

    print(
        f"Watching {', '.join(COURSES)} (term {TERM}). "
        f"Checking every {args.interval}s. Press Ctrl+C to stop."
    )
    try:
        while True:
            check_once(args.topic, state, section_filter)
            save_state(state)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
