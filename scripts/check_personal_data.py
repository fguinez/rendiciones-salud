"""Fail when tracked files contain personal data: RUTs, email addresses or home paths.

Usage: ``python scripts/check_personal_data.py [PATH ...]`` (defaults to ``git ls-files``), or
``--history`` to scan every line ever added in the git history and every commit message.
Dependency-free on purpose, so it runs before anything is installed. Findings are masked
so the CI log does not republish what it caught.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

ALLOWED_RUTS = frozenset(
    {
        "11111111-1",
        "11111111-2",
        "22222222-2",
        "22333444-K",
        "76123456-0",
        "77654321-7",
        "12345678-9",
        "12345678-5",
        "15555555-6",
        "76296619-0",  # Isapre Colmena (public company RUT)
        "77408820-2",  # Chubb Seguros (public company RUT)
    }
)
"""Placeholder RUTs used by tests and docs, plus the insurers' public RUTs, as ``body-DV``."""

ALLOWED_EMAIL_DOMAINS = frozenset({"example.com", "example.org", "users.noreply.github.com"})
"""Email domains that never belong to a real person."""

ALLOWED_EMAILS = frozenset({"noreply@github.com", "noreply@anthropic.com", "git@github.com"})
"""Service addresses (commit trailers, SSH remotes) that are not personal."""

SKIPPED_FILES = frozenset({"uv.lock"})
"""Tracked files too large or too generated to be worth scanning."""

DASH = "[-\u2010-\u2014\u2212]"
RUT_RE = re.compile(rf"(?<![\w.])(\d{{1,2}}\.\d{{3}}\.\d{{3}}|\d{{7,8}}) ?{DASH} ?([0-9kK])\b")
BARE_RUT_RE = re.compile(rf"(?<![\w.])([1-9]\d{{7}})([0-9kK])(?!\w|\.\d| ?{DASH})")
EMAIL_RE = re.compile(r"[\w.%+-]+@(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}")
HOME_PATH_RE = re.compile(r"(?<![\w.])/(?:Users|home)/[\w.-]+")

Rule = Callable[[str], Iterator[str]]


def compute_check_digit(body: str) -> str:
    """Return the mod-11 check digit of a RUT body."""
    total = sum(int(digit) * (2 + index % 6) for index, digit in enumerate(reversed(body)))
    remainder = 11 - total % 11
    return {11: "0", 10: "K"}.get(remainder, str(remainder))


def find_ruts(line: str) -> Iterator[str]:
    """Non-allowlisted RUTs: with any dash, or 9 bare digits with a valid check digit."""
    for match in RUT_RE.finditer(line):
        canonical = f"{match[1].replace('.', '')}-{match[2].upper()}"
        if canonical not in ALLOWED_RUTS:
            yield match[0]
    for match in BARE_RUT_RE.finditer(line):
        body, digit = match[1], match[2].upper()
        if compute_check_digit(body) == digit and f"{body}-{digit}" not in ALLOWED_RUTS:
            yield match[0]


def find_emails(line: str) -> Iterator[str]:
    """Email addresses outside example.com/example.org and GitHub noreply addresses."""
    for match in EMAIL_RE.finditer(line):
        address = match[0].lower()
        domain = address.rsplit("@", 1)[1]
        if address not in ALLOWED_EMAILS and domain not in ALLOWED_EMAIL_DOMAINS:
            yield match[0]


def find_home_paths(line: str) -> Iterator[str]:
    """Absolute home paths such as /Users/<name>/ or /home/<name>/."""
    yield from (match[0] for match in HOME_PATH_RE.finditer(line))


RULES: dict[str, Rule] = {"rut": find_ruts, "email": find_emails, "home-path": find_home_paths}


def mask(value: str) -> str:
    """Keep the first characters of a finding so it can be located but not reused."""
    keep = min(4, len(value) // 3)
    return value[:keep] + "*" * (len(value) - keep)


def scan_text(text: str) -> list[tuple[int, str, str]]:
    """Return ``(line number, rule, finding)`` for every personal-data hit in ``text``."""
    return [
        (number, name, finding)
        for number, line in enumerate(text.splitlines(), start=1)
        for name, rule in RULES.items()
        for finding in rule(line)
    ]


def read_text(path: Path) -> str | None:
    """Return the file's text (non-UTF-8 bytes replaced), or None for binaries and missing files."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    if b"\0" in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace")


def run_git(*args: str) -> str:
    """Run git and return its stdout, decoded leniently."""
    git = shutil.which("git")
    if git is None:
        raise SystemExit("git no está instalado")
    result = subprocess.run([git, *args], capture_output=True, check=True)
    return result.stdout.decode("utf-8", errors="replace")


def list_tracked_files() -> list[str]:
    """Return the repository's tracked files, relative to the current directory."""
    return [name for name in run_git("ls-files", "-z").split("\0") if name]


def parse_diff_path(header: str) -> str:
    """Return the file path of a ``+++`` diff header, or "" for a deleted file."""
    header = header.rstrip("\t").strip('"')
    if header == "/dev/null":
        return ""
    return header.removeprefix("b/")


def scan_history(log: str) -> Iterator[tuple[str, str, str, str]]:
    """Yield ``(commit, file, rule, finding)`` for each hit on an added line of ``git log -p``."""
    commit = path = ""
    for line in log.splitlines():
        if line.startswith("commit "):
            commit = line.split()[1][:8]
        elif line.startswith("+++ "):
            path = parse_diff_path(line[4:])
        elif line.startswith("+") and path and Path(path).name not in SKIPPED_FILES:
            for name, rule in RULES.items():
                for finding in rule(line[1:]):
                    yield commit, path, name, finding


def scan_messages(log: str) -> Iterator[tuple[str, str, str, str]]:
    """Yield ``(commit, "<message>", rule, finding)`` for each hit in NUL-separated messages."""
    fields = log.split("\0")
    for sha, message in zip(fields[0::2], fields[1::2], strict=False):
        for line in message.splitlines():
            for name, rule in RULES.items():
                for finding in rule(line):
                    yield sha.strip()[:8], "<message>", name, finding


def main_history() -> int:
    """Scan every added line and every commit message in the history; print ``commit:file``."""
    diffs = run_git(
        "-c",
        "core.quotePath=false",
        "log",
        "-p",
        "--all",
        "--no-color",
        "--no-ext-diff",
        "--format=commit %H",
    )
    messages = run_git("log", "--all", "--format=%H%x00%B%x00")
    findings = 0
    for commit, path, rule, finding in [*scan_history(diffs), *scan_messages(messages)]:
        print(f"{commit}:{path}: {rule}: {mask(finding)}")
        findings += 1
    if findings:
        print(f"\n{findings} posible(s) dato(s) personal(es) en el historial.")
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Scan the given paths (or every tracked file) and print ``file:line`` per finding."""
    if argv and argv[0] == "--history":
        return main_history()
    paths = list(argv) if argv else list_tracked_files()
    findings = 0
    for name in paths:
        if Path(name).name in SKIPPED_FILES:
            continue
        text = read_text(Path(name))
        if text is None:
            continue
        for number, rule, finding in scan_text(text):
            print(f"{name}:{number}: {rule}: {mask(finding)}")
            findings += 1
    if findings:
        print(f"\n{findings} posible(s) dato(s) personal(es). Reemplázalos por datos ficticios.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
