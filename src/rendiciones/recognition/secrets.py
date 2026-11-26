"""API key lookup: environment first, then the macOS Keychain. Never persisted."""

from __future__ import annotations

import os
import subprocess
import sys

KEYCHAIN_SERVICE = "rendiciones-salud"
KEYCHAIN_ACCOUNT = "anthropic"
SECURITY_BIN = "/usr/bin/security"


def anthropic_api_key() -> str | None:
    """Return the Anthropic API key from ``ANTHROPIC_API_KEY`` or the Keychain."""
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        return key
    if sys.platform != "darwin":
        return None
    try:
        result = subprocess.run(
            [
                SECURITY_BIN,
                "find-generic-password",
                "-s",
                KEYCHAIN_SERVICE,
                "-a",
                KEYCHAIN_ACCOUNT,
                "-w",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None
