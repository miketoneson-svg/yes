#!/usr/bin/env python3
"""Concurrent username checker: TikTok, Xbox Live, Discord.

Run with no arguments for an interactive menu, or use flags:
  python checker.py --name targetuser
  python checker.py --name alpha beta gamma
  python checker.py --file names.txt --concurrency 6
  python checker.py --gen 4l --count 50                   # random 4-letter names
  python checker.py --gen 4c --style pronounceable        # 4 chars, letters + digits

Optional (environment variable, or enter it in the menu under Settings):
1ef9b12c-2f23-4efa-8903-89d0a5d62de9   Xbox lookups through OpenXBL (https://xbl.io). Without it,
                    Xbox falls back to a best-effort third-party page check.
"""

import argparse
import asyncio
import getpass
import os
import random
import re
import string
import sys
import time
from collections import Counter
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from urllib.parse import quote

import aiohttp
from colorama import Fore, Style, init as colorama_init

TIMEOUT = aiohttp.ClientTimeout(total=5)  # strict 5s cap per request

TIKTOK_URL = "https://www.tiktok.com/@{name}"
XBL_SEARCH_URL = "https://xbl.io/api/v2/search/{name}"
XBOX_FALLBACK_URL = "https://xboxgamertag.com/search/{name}"
DISCORD_URL = "https://discord.com/api/v9/unique-username/username-attempt-unauthed"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
]


class Status(Enum):
    AVAILABLE = "available"
    TAKEN = "taken"
    ERROR = "error"


@dataclass
class Result:
    platform: str
    status: Status
    detail: str = ""


def make_headers(extra=None):
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if extra:
        headers.update(extra)
    return headers


def classify_profile_page(platform, code, note=""):
    """Shared logic for 'does a profile page exist?' style checks."""
    if code == 200:
        result = Result(platform, Status.TAKEN, "profile exists")
    elif code == 404:
        result = Result(platform, Status.AVAILABLE, "no profile found")
    elif code in (403, 429):
        result = Result(platform, Status.ERROR, f"blocked or rate-limited (HTTP {code})")
    else:
        result = Result(platform, Status.ERROR, f"unexpected HTTP {code}")
    if note:
        result.detail += f" ({note})"
    return result


# ---------------------------------------------------------------- validators
# Return an error string if the name can't exist on that platform, else None.

def validate_tiktok(n):
    if not re.fullmatch(r"[A-Za-z0-9_.]{2,24}", n) or n.endswith("."):
        return "invalid: 2-24 chars, letters/numbers/_/. (can't end in .)"


def validate_xbox(n):
    if not re.fullmatch(r"[A-Za-z0-9 ]{1,15}", n):
        return "invalid: 1-15 chars, letters/numbers/spaces"


def validate_discord(n):
    n = n.lower()
    if not re.fullmatch(r"[a-z0-9_.]{2,32}", n) or ".." in n:
        return "invalid: 2-32 chars, a-z/0-9/_/. (no consecutive dots)"


# ------------------------------------------------------------ shared context

class Pacer:
    """Spaces out request starts per platform so big batches don't trip rate limits."""

    def __init__(self, delay):
        self.delay = delay
        self._next = {}

    async def wait(self, platform):
        if self.delay <= 0:
            return
        now = time.monotonic()
        start = max(now, self._next.get(platform, 0.0))
        self._next[platform] = start + self.delay  # reserve our slot (no await above)
        if start > now:
            await asyncio.sleep(start - now)


@dataclass
class Context:
    session: aiohttp.ClientSession
    xbl_key: str = ""
    pacer: Pacer = None


# ------------------------------------------------------------------ checkers

async def check_tiktok(ctx, name):
    url = TIKTOK_URL.format(name=quote(name))
    async with ctx.session.get(url, headers=make_headers(), timeout=TIMEOUT) as resp:
        return classify_profile_page("TikTok", resp.status)


def find_gamertag(node, name):
    """True if any gamertag field anywhere in an OpenXBL response equals `name`."""
    target = name.casefold()
    if isinstance(node, dict):
        for key, value in node.items():
            if (isinstance(key, str) and key.lower() == "gamertag"
                    and isinstance(value, str) and value.casefold() == target):
                return True
        # settings-list shape: {"id": "Gamertag", "value": "..."}
        if (str(node.get("id", "")).lower() == "gamertag"
                and isinstance(node.get("value"), str)
                and node["value"].casefold() == target):
            return True
        return any(find_gamertag(v, name) for v in node.values())
    if isinstance(node, list):
        return any(find_gamertag(v, name) for v in node)
    return False


async def check_xbox(ctx, name):
    if not ctx.xbl_key:
        # No official unauthenticated lookup exists; best-effort page check.
        url = XBOX_FALLBACK_URL.format(name=quote(name))
        async with ctx.session.get(url, headers=make_headers(), timeout=TIMEOUT) as resp:
            return classify_profile_page("Xbox Live", resp.status, "unverified: no OpenXBL key")
    url = XBL_SEARCH_URL.format(name=quote(name))
    headers = make_headers({"X-Authorization": ctx.xbl_key, "Accept": "application/json"})
    async with ctx.session.get(url, headers=headers, timeout=TIMEOUT) as resp:
        if resp.status == 200:
            data = await resp.json(content_type=None)
            if find_gamertag(data, name):
                return Result("Xbox Live", Status.TAKEN, "exact gamertag match (OpenXBL)")
            return Result("Xbox Live", Status.AVAILABLE, "no exact match (OpenXBL)")
        if resp.status in (401, 403):
            return Result("Xbox Live", Status.ERROR, f"OpenXBL key rejected (HTTP {resp.status})")
        if resp.status == 429:
            return Result("Xbox Live", Status.ERROR, "OpenXBL rate limit hit (HTTP 429)")
        return Result("Xbox Live", Status.ERROR, f"unexpected HTTP {resp.status} from OpenXBL")


async def check_discord(ctx, name):
    # Endpoint used by Discord's registration page for the unique-username system.
    headers = make_headers({
        "Accept": "application/json",
        "Origin": "https://discord.com",
        "Referer": "https://discord.com/register",
    })
    async with ctx.session.post(DISCORD_URL, json={"username": name.lower()},
                                headers=headers, timeout=TIMEOUT) as resp:
        if resp.status == 200:
            data = await resp.json(content_type=None)
            taken = data.get("taken") if isinstance(data, dict) else None
            if taken is True:
                return Result("Discord", Status.TAKEN, "username in use")
            if taken is False:
                return Result("Discord", Status.AVAILABLE, "username free")
            return Result("Discord", Status.ERROR, "unexpected response body")
        if resp.status == 429:
            return Result("Discord", Status.ERROR, "rate limited (HTTP 429)")
        if resp.status in (401, 403):
            return Result("Discord", Status.ERROR, f"blocked (HTTP {resp.status})")
        return Result("Discord", Status.ERROR, f"unexpected HTTP {resp.status}")


PLATFORMS = [
    ("TikTok", validate_tiktok, check_tiktok),
    ("Xbox Live", validate_xbox, check_xbox),
    ("Discord", validate_discord, check_discord),
]


# --------------------------------------------------------------- orchestration

async def safe_check(ctx, sem, platform, validator, checker, name):
    """Run one platform check (paced, under the shared semaphore); never raise."""
    problem = validator(name)
    if problem:
        return Result(platform, Status.ERROR, problem)
    try:
        await ctx.pacer.wait(platform)  # pace first so waiting never holds a semaphore slot
        async with sem:
            return await checker(ctx, name)
    except asyncio.TimeoutError:
        return Result(platform, Status.ERROR, "timed out after 5s")
    except aiohttp.ClientConnectorError:
        return Result(platform, Status.ERROR, "connection or DNS error")
    except aiohttp.ClientError as exc:
        return Result(platform, Status.ERROR, f"network error ({type(exc).__name__})")
    except Exception as exc:  # last-resort safety net
        return Result(platform, Status.ERROR, f"unexpected error: {exc}")


async def check_name(ctx, sem, name):
    results = await asyncio.gather(
        *(safe_check(ctx, sem, p, v, c, name) for p, v, c in PLATFORMS))
    return name, list(results)


async def run(names, concurrency, report, delay=0.0, xbl_key=None):
    """Check every name; `report(name, results)` fires as each name finishes."""
    if xbl_key is None:
        xbl_key = os.environ.get("OPENXBL_API_KEY", "").strip()
    sem = asyncio.Semaphore(concurrency)  # caps in-flight requests across all names
    async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
        ctx = Context(session=session, xbl_key=xbl_key, pacer=Pacer(delay))
        tasks = [asyncio.create_task(check_name(ctx, sem, n)) for n in names]
        collected = {}
        for fut in asyncio.as_completed(tasks):
            name, results = await fut
            collected[name] = results
            report(name, results)
    return collected


# ----------------------------------------------------------------- generation

LETTERS = string.ascii_lowercase
ALNUM = string.ascii_lowercase + string.digits
TOKENS = {"C": "bcdfghjklmnprstvwz", "V": "aeiou", "D": string.digits}
PRONOUNCEABLE_PATTERNS = {
    "4l": ["CVCV", "VCVC", "CVVC", "CVCC"],
    "4c": ["CVCD", "CVDD", "DCVC"],
}


def generate_one(kind, style):
    """4l = four letters; 4c = four characters from letters + digits."""
    if style == "pronounceable":
        pattern = random.choice(PRONOUNCEABLE_PATTERNS[kind])
        return "".join(random.choice(TOKENS[t]) for t in pattern)
    pool = LETTERS if kind == "4l" else ALNUM
    return "".join(random.choice(pool) for _ in range(4))


def generate_names(kind, count, style, exclude=()):
    """Return up to `count` unique new names, skipping anything in `exclude`."""
    seen = {n.casefold() for n in exclude}
    names = []
    for _ in range(count * 50 + 1000):  # bounded in case the pool is small
        if len(names) >= count:
            break
        name = generate_one(kind, style)
        if name not in seen:
            seen.add(name)
            names.append(name)
    return names


# ------------------------------------------------------------- name handling

def clean_name(raw):
    return raw.strip().lstrip("@")


def dedupe_names(raw_names):
    seen, names = set(), []
    for item in raw_names:
        name = clean_name(item)
        if name and name.casefold() not in seen:
            seen.add(name.casefold())
            names.append(name)
    return names


def read_name_file(path):
    """One name per line; blank lines and # comments are skipped."""
    lines = Path(path).expanduser().read_text(encoding="utf-8-sig").splitlines()
    return [ln for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]


# ------------------------------------------------------------------ terminal

LABELS = {
    Status.AVAILABLE: (Fore.LIGHTGREEN_EX, "[+] Available"),
    Status.TAKEN: (Fore.LIGHTRED_EX, "[-] Taken"),
    Status.ERROR: (Fore.LIGHTYELLOW_EX, "[!] Blocked/Error"),
}
SHORT = {
    Status.AVAILABLE: (Fore.LIGHTGREEN_EX, "[+]"),
    Status.TAKEN: (Fore.LIGHTRED_EX, "[-]"),
    Status.ERROR: (Fore.LIGHTYELLOW_EX, "[!]"),
}


def print_block(name, results):
    print(f"{Style.BRIGHT}{name}{Style.RESET_ALL}")
    for r in results:
        color, label = LABELS[r.status]
        print(f"  {r.platform:<10} {Style.BRIGHT}{color}{label:<19}{Style.RESET_ALL} "
              f"{Style.DIM}{r.detail}{Style.RESET_ALL}")
    print()


def print_compact(name, results):
    cells = []
    for r in results:
        color, symbol = SHORT[r.status]
        cells.append(f"{r.platform} {Style.BRIGHT}{color}{symbol}{Style.RESET_ALL}")
    print(f"{Style.BRIGHT}{name:<16}{Style.RESET_ALL}" + "  ".join(cells))


def print_legend():
    print(f"{Style.BRIGHT}{Fore.LIGHTGREEN_EX}[+]{Style.RESET_ALL} available   "
          f"{Style.BRIGHT}{Fore.LIGHTRED_EX}[-]{Style.RESET_ALL} taken   "
          f"{Style.BRIGHT}{Fore.LIGHTYELLOW_EX}[!]{Style.RESET_ALL} blocked/error\n")


def print_summary(names, collected):
    hits, unverified = [], False
    for n in names:
        free = []
        for r in collected[n]:
            if r.status is Status.AVAILABLE:
                mark = "*" if "unverified" in r.detail else ""
                unverified = unverified or bool(mark)
                free.append(r.platform + mark)
        if free:
            hits.append((n, free))
    hits.sort(key=lambda h: -len(h[1]))  # stable: ties keep input order
    everywhere = sum(1 for _, f in hits if len(f) == len(PLATFORMS))
    print(f"\n{Style.BRIGHT}Summary:{Style.RESET_ALL} {len(names)} names checked, "
          f"{everywhere} available on every platform, "
          f"{len(hits)} available on at least one.")
    for n, free in hits:
        color = Fore.LIGHTGREEN_EX if len(free) == len(PLATFORMS) else ""
        print(f"  {Style.BRIGHT}{color}{n:<16}{Style.RESET_ALL}{', '.join(free)}")
    if unverified:
        print(f"{Style.DIM}  * unverified page check (no OpenXBL key set){Style.RESET_ALL}")
    problems = Counter((r.platform, r.detail) for n in names
                       for r in collected[n] if r.status is Status.ERROR)
    if problems:
        print(f"\n{Style.BRIGHT}{Fore.LIGHTYELLOW_EX}Blocked/error reasons:{Style.RESET_ALL}")
        for (platform, detail), k in problems.most_common(5):
            print(f"  {platform}: {detail} (x{k})")
    print()


# ---------------------------------------------------------- settings + runner

@dataclass
class Settings:
    style: str = "random"       # generated-name style
    count: int = 20             # default number of generated names
    concurrency: int = 5        # max simultaneous requests
    delay: float = 0.5          # min seconds between requests to one platform
    verbose: bool = False       # full detail even for large batches
    xbl_key: str = ""           # OpenXBL API key


def describe_sources(xbl_on):
    xbox = "OpenXBL API" if xbl_on else "best-effort page check"
    return f"Sources - Xbox: {xbox} | TikTok, Discord: direct"


def check_and_report(names, settings):
    """Run the checks for `names` and print results plus a summary."""
    print(f"\n{Style.DIM}{describe_sources(bool(settings.xbl_key))}{Style.RESET_ALL}")
    if len(names) > 100:
        print(f"{Style.DIM}Large batch: free API tiers are rate-limited, "
              f"so expect some yellow results.{Style.RESET_ALL}")
    print(f"Checking {len(names)} username{'s' if len(names) != 1 else ''}...\n")
    compact = len(names) > 5 and not settings.verbose
    if compact:
        print_legend()
    collected = asyncio.run(run(names, settings.concurrency,
                                print_compact if compact else print_block,
                                settings.delay, settings.xbl_key))
    if len(names) > 1:
        print_summary(names, collected)


# ---------------------------------------------------------------------- menu

def ask(prompt):
    return input(prompt).strip()


def ask_number(prompt, default, cast, minimum):
    """Blank = default. Returns None (after a message) on invalid input."""
    raw = ask(prompt)
    if not raw:
        return default
    try:
        value = cast(raw)
    except ValueError:
        print(f"{Fore.LIGHTYELLOW_EX}That isn't a valid number.{Style.RESET_ALL}\n")
        return None
    if value < minimum:
        print(f"{Fore.LIGHTYELLOW_EX}Please enter {minimum} or more.{Style.RESET_ALL}\n")
        return None
    return value


def pause():
    ask("\nPress Enter to return to the menu...")


def print_menu(settings):
    bar = "=" * 46
    cyan = Style.BRIGHT + Fore.LIGHTCYAN_EX
    print(f"\n{cyan}{bar}{Style.RESET_ALL}")
    print(f"{cyan}  USERNAME CHECKER{Style.RESET_ALL}")
    print(f"{cyan}  TikTok | Xbox Live | Discord{Style.RESET_ALL}")
    print(f"{cyan}{bar}{Style.RESET_ALL}")
    print("  1) Check username(s)")
    print("  2) Check names from a file")
    print("  3) Generate and check 4-letter (4l) names")
    print("  4) Generate and check 4-character (4c) names")
    print("  5) Settings")
    print("  6) Quit")
    key = "OpenXBL key set" if settings.xbl_key else "no OpenXBL key (Xbox is unverified)"
    print(f"{Style.DIM}  {key}{Style.RESET_ALL}\n")


def menu_check_names(settings):
    raw = ask("Username(s) - separate with spaces, or commas if a name has spaces\n"
              "(blank to go back): ")
    parts = raw.split(",") if "," in raw else raw.split()
    names = dedupe_names(parts)
    if not names:
        return
    check_and_report(names, settings)
    pause()


def menu_check_file(settings):
    path = ask("Path to names file (blank to go back): ").strip("'\"")
    if not path:
        return
    try:
        names = dedupe_names(read_name_file(path))
    except (OSError, UnicodeDecodeError) as exc:
        reason = getattr(exc, "strerror", None) or "not a readable text file"
        print(f"{Fore.LIGHTYELLOW_EX}Couldn't read that file: {reason}{Style.RESET_ALL}\n")
        return
    if not names:
        print(f"{Fore.LIGHTYELLOW_EX}No usernames found in that file.{Style.RESET_ALL}\n")
        return
    check_and_report(names, settings)
    pause()


def menu_generate(settings, kind):
    label = "4-letter (4l)" if kind == "4l" else "4-character (4c)"
    count = ask_number(f"How many {label} names? [{settings.count}]: ",
                       settings.count, int, 1)
    if count is None:
        return
    names = generate_names(kind, count, settings.style)
    print(f"\nGenerated {len(names)} {settings.style} {kind} names.")
    check_and_report(names, settings)
    pause()


def menu_settings(settings):
    while True:
        key = "set" if settings.xbl_key else "not set"
        print(f"\n{Style.BRIGHT}Settings{Style.RESET_ALL}")
        print(f"  1) Generated name style ........ {settings.style}")
        print(f"  2) Default generate count ...... {settings.count}")
        print(f"  3) Max simultaneous requests ... {settings.concurrency}")
        print(f"  4) Delay per platform (sec) .... {settings.delay}")
        print(f"  5) OpenXBL API key ............. {key}")
        print(f"  6) Output detail ............... {'full' if settings.verbose else 'compact for 6+ names'}")
        print("  7) Back")
        choice = ask("Change which setting? [1-7]: ")
        if choice == "1":
            settings.style = "pronounceable" if settings.style == "random" else "random"
        elif choice == "2":
            value = ask_number("New default count: ", settings.count, int, 1)
            settings.count = settings.count if value is None else value
        elif choice == "3":
            value = ask_number("Max simultaneous requests: ", settings.concurrency, int, 1)
            settings.concurrency = settings.concurrency if value is None else value
        elif choice == "4":
            value = ask_number("Delay in seconds (0 = none): ", settings.delay, float, 0)
            settings.delay = settings.delay if value is None else value
        elif choice == "5":
            new_key = getpass.getpass("OpenXBL API key (blank = keep, 'clear' = remove): ").strip()
            if new_key.lower() == "clear":
                settings.xbl_key = ""
            elif new_key:
                settings.xbl_key = new_key
        elif choice == "6":
            settings.verbose = not settings.verbose
        elif choice in ("7", ""):
            return
        else:
            print(f"{Fore.LIGHTYELLOW_EX}Please enter a number from 1 to 7.{Style.RESET_ALL}")


ACTIONS = {
    "1": menu_check_names,
    "2": menu_check_file,
    "3": lambda s: menu_generate(s, "4l"),
    "4": lambda s: menu_generate(s, "4c"),
    "5": menu_settings,
}


def run_menu(settings):
    while True:
        print_menu(settings)
        try:
            choice = ask("Choose an option [1-6]: ").lower()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if choice in ("6", "q", "quit", "exit"):
            break
        action = ACTIONS.get(choice)
        if action is None:
            print(f"{Fore.LIGHTYELLOW_EX}Please enter a number from 1 to 6.{Style.RESET_ALL}")
            continue
        try:
            action(settings)
        except KeyboardInterrupt:
            print("\n\nCancelled.")
        except EOFError:
            print()
            break
    print("Bye.")


# ----------------------------------------------------------------------- CLI

class FriendlyParser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        print(f"\nError: {message}\n"
              "Examples:\n"
              "  python checker.py                      (opens the menu)\n"
              "  python checker.py --name targetuser\n"
              "  python checker.py --file names.txt --concurrency 6\n"
              "  python checker.py --gen 4l --count 50",
              file=sys.stderr)
        sys.exit(2)


def load_names(cli_names, file_path, parser):
    raw = list(cli_names or [])
    if file_path:
        try:
            raw += read_name_file(file_path)
        except (OSError, UnicodeDecodeError) as exc:
            reason = getattr(exc, "strerror", None) or "not a readable text file"
            parser.error(f"cannot read {file_path}: {reason}")
    return dedupe_names(raw)


def main():
    colorama_init(autoreset=False)
    parser = FriendlyParser(
        description="Check username availability on TikTok, Xbox Live and Discord. "
                    "Run with no name options to open the interactive menu.")
    parser.add_argument("-n", "--name", nargs="+", metavar="USERNAME",
                        help="one or more usernames to check")
    parser.add_argument("-f", "--file", metavar="PATH",
                        help="text file with one username per line (# comments allowed)")
    parser.add_argument("-g", "--gen", choices=["4l", "4c"],
                        help="generate names to check: 4l = four letters, "
                             "4c = four characters (letters + digits)")
    parser.add_argument("--count", type=int, default=20, metavar="N",
                        help="how many names to generate with --gen (default: 20)")
    parser.add_argument("--style", choices=["random", "pronounceable"], default="random",
                        help="style of generated names (default: random)")
    parser.add_argument("-c", "--concurrency", type=int, default=5, metavar="N",
                        help="max simultaneous requests across all names (default: 5)")
    parser.add_argument("-d", "--delay", type=float, default=0.5, metavar="SEC",
                        help="minimum seconds between requests to the same platform "
                             "(default: 0.5)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="full per-platform detail even for large batches")
    args = parser.parse_args()

    if args.concurrency < 1:
        parser.error("--concurrency must be at least 1")
    if args.count < 1:
        parser.error("--count must be at least 1")
    if args.delay < 0:
        parser.error("--delay cannot be negative")

    if sys.platform == "win32" and sys.version_info < (3, 14):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    settings = Settings(style=args.style, count=args.count, concurrency=args.concurrency,
                        delay=args.delay, verbose=args.verbose,
                        xbl_key=os.environ.get("OPENXBL_API_KEY", "").strip())

    if not (args.name or args.file or args.gen):
        run_menu(settings)
        return

    names = load_names(args.name, args.file, parser) if (args.name or args.file) else []
    if args.gen:
        generated = generate_names(args.gen, args.count, args.style, exclude=names)
        if len(generated) < args.count:
            print(f"Only {len(generated)} unique {args.gen} names available "
                  f"for style '{args.style}'.", file=sys.stderr)
        names += generated
    if not names:
        parser.error("no usernames found to check")
    check_and_report(names, settings)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(130)
