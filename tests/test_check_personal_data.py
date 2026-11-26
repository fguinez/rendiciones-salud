"""Tests for scripts/check_personal_data.py.

Personal-looking values are assembled at runtime so this file passes its own scan.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "check_personal_data.py"


def load_script() -> ModuleType:
    """Import the script as a module (scripts/ is not a package)."""
    spec = importlib.util.spec_from_file_location("check_personal_data", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check = load_script()


@pytest.mark.parametrize(
    ("line", "rule"),
    [
        ("RUT " + "98.765." + "432-1", "rut"),
        ("rut:" + "9876543" + "2-K", "rut"),
        ("paciente " + "9876543" + " - 2", "rut"),
        ("RUT: " + "98.765." + "432\u22121", "rut"),
        ('value="' + "98765432" + '5"', "rut"),
        ("mail ana.perez" + "@" + "gmail.com", "email"),
        ("cd " + "/Users/" + "ana/proyectos", "home-path"),
        ("HOME=" + "/home/" + "bob", "home-path"),
    ],
)
def test_scan_text_flags_personal_data(line: str, rule: str) -> None:
    """RUTs, real email addresses and home paths are reported on their line."""
    text = f"primera línea\n{line}\n"

    findings = check.scan_text(text)

    assert [(number, name) for number, name, _ in findings] == [(2, rule)]


@pytest.mark.parametrize(
    "line",
    [
        "11.111.111-1 y 11111111-1",
        "22.333.444-k",
        "15555555 - 6",
        "Colmena 76.296.619-0, Chubb 77408820-2",
        "ana@example.com, soporte@example.org",
        "12345+bot@users.noreply.github.com",
        "Co-Authored-By: Claude <noreply@anthropic.com>",
        "folio 123456789-0, fecha 2026-11-05, monto $30.000",
        'RUT 22.333.444\u2212K, beneficiario value="222222222"',
        "solicitud 987654322, cuenta 000000000, request 44556677",
        "~/Library/Application Support y /Users/<nombre>/",
        "https://example.com/home/inicio",
    ],
)
def test_scan_text_allows_placeholders(line: str) -> None:
    """Allowlisted placeholders and look-alikes that are not personal data pass."""
    findings = check.scan_text(line)

    assert findings == []


def test_main_reports_file_and_line_masked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """main() exits 1 and prints ``file:line`` with the finding masked."""
    secret = "98.765." + "432-1"
    path = tmp_path / "notas.md"
    path.write_text(f"ok 11.111.111-1\nRUT {secret}\n", encoding="utf-8")

    code = check.main([str(path)])

    output = capsys.readouterr().out
    assert code == 1
    assert f"{path}:2: rut:" in output
    assert secret not in output


def test_main_skips_binary_and_lock_files(tmp_path: Path) -> None:
    """Binary files and uv.lock are not scanned."""
    secret = ("98.765." + "432-1").encode()
    binary = tmp_path / "foto.png"
    binary.write_bytes(b"\x89PNG\0" + secret)
    lock = tmp_path / "uv.lock"
    lock.write_bytes(secret)

    code = check.main([str(binary), str(lock)])

    assert code == 0


def test_main_scans_non_utf8_files(tmp_path: Path) -> None:
    """A latin-1 file is still scanned instead of being skipped."""
    path = tmp_path / "cartola.csv"
    path.write_bytes(("Descripción;RUT " + "98.765." + "432-1\n").encode("latin-1"))

    code = check.main([str(path)])

    assert code == 1


def test_scan_history_reports_added_lines_only() -> None:
    """Only lines added in a commit are reported, with the commit and file they came from."""
    secret = "98.765." + "432-1"
    log = "\n".join(
        [
            "commit 0123456789abcdef",
            "diff --git a/notas.md b/notas.md",
            "--- a/notas.md",
            "+++ b/notas.md",
            f"-RUT {secret}",
            f"+RUT {secret}",
            " contexto 11.111.111-1",
        ]
    )

    findings = list(check.scan_history(log))

    assert [(commit, path, rule) for commit, path, rule, _ in findings] == [
        ("01234567", "notas.md", "rut")
    ]


@pytest.mark.parametrize(
    "header",
    ["+++ b/rendición.md", '+++ "b/rendici\\303\\263n.md"', "+++ b/mi archivo.md\t"],
)
def test_scan_history_reads_quoted_and_unicode_paths(header: str) -> None:
    """Files whose diff header git quotes or pads are scanned and reported by their path."""
    log = "\n".join(["commit 0123456789abcdef", header, "+RUT " + "98.765." + "432-1"])

    findings = list(check.scan_history(log))

    assert [path for _, path, _, _ in findings] == [check.parse_diff_path(header[4:])]
    assert not findings[0][1].endswith(("\t", '"'))


def test_scan_messages_reports_commit_messages() -> None:
    """A RUT or email in a commit message is reported with its commit."""
    log = "abcdef0123\0Fix\n\nRUT " + "98.765." + "432-1\n\0\nfedcba9876\0Ok\n\0\n"

    findings = list(check.scan_messages(log))

    assert [(commit, rule) for commit, _, rule, _ in findings] == [("abcdef01", "rut")]
