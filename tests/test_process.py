from __future__ import annotations

import subprocess
import sys

import pytest

#: Starts with a secret in its initial environment, the way the bot does, then
#: has a child running as the same user try to read it from /proc.
PROBE = """
import os, subprocess, sys
from doctovid.hardening import make_undumpable
if sys.argv[1] == "undumpable":
    assert make_undumpable()
script = "tr '\\\\0' '\\\\n' < /proc/$PPID/environ | grep -c BOT_TOKEN || true"
print(subprocess.run(["sh", "-c", script], env={"PATH": os.environ["PATH"]},
                     capture_output=True, text=True).stdout.strip() or "0")
"""


def secrets_readable_by_a_child(mode: str) -> bool:
    result = subprocess.run(
        [sys.executable, "-c", PROBE, mode],
        env={"BOT_TOKEN": "123:secret", "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip() != "0"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux /proc only")
def test_an_undumpable_bot_hides_its_environment_from_ffmpeg() -> None:
    # The control: without the fix, a clean child environment is not enough.
    assert secrets_readable_by_a_child("plain")
    assert not secrets_readable_by_a_child("undumpable")
