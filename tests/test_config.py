from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from doctovid.__main__ import secure_data_dir
from doctovid.config import TELEGRAM_MAX_FILE_MB, ConfigError, load
from doctovid.store import Store

REQUIRED = {"API_ID": "12345", "API_HASH": "abc", "BOT_TOKEN": "1:x"}


def test_defaults() -> None:
    config = load(REQUIRED)
    assert config.api_id == 12345
    assert config.admin_ids == frozenset()
    assert config.cache_channel_id is None
    assert config.max_file_mb == TELEGRAM_MAX_FILE_MB
    assert config.workers == 2
    assert config.session_path == Path("data/bot.session")


def test_every_problem_is_reported_at_once() -> None:
    with pytest.raises(ConfigError) as caught:
        load({"API_ID": "twelve", "WORKERS": "0", "CACHE_CHANNEL_ID": "12345"})
    problems = "\n".join(caught.value.problems)
    for expected in (
        "API_ID must be a whole number",
        "API_HASH is required",
        "BOT_TOKEN is required",
        "WORKERS must be between 1 and 16",
        "CACHE_CHANNEL_ID must be a channel id starting with -100",
    ):
        assert expected in problems


def test_admins_and_channel() -> None:
    config = load({**REQUIRED, "ADMIN_IDS": "1, 2,3", "CACHE_CHANNEL_ID": "-1009876"})
    assert config.admin_ids == frozenset({1, 2, 3})
    assert config.cache_channel_id == -1009876


def test_file_limit_cannot_exceed_telegrams() -> None:
    with pytest.raises(ConfigError, match="MAX_FILE_MB"):
        load({**REQUIRED, "MAX_FILE_MB": str(TELEGRAM_MAX_FILE_MB + 1)})


def test_limits_have_defaults_and_can_be_turned_off() -> None:
    config = load({**REQUIRED, "USER_DAILY_GB": "0"})
    assert config.user_limits.files_per_day == 20
    assert config.user_limits.gb_per_day == 0
    assert config.admin_limits.files_per_day == 100


def test_an_unknown_time_zone_is_refused() -> None:
    with pytest.raises(ConfigError, match="TIMEZONE"):
        load({**REQUIRED, "TIMEZONE": "Mars/Olympus"})


def test_join_channel_by_username_or_id() -> None:
    public = load({**REQUIRED, "JOIN_CHANNEL": "@newsroom"})
    assert (public.join_channel, public.join_link) == ("newsroom", "https://t.me/newsroom")

    private = load(
        {**REQUIRED, "JOIN_CHANNEL": "-1001234", "JOIN_CHANNEL_LINK": "https://t.me/+abc"}
    )
    assert (private.join_channel, private.join_link) == (-1001234, "https://t.me/+abc")

    with pytest.raises(ConfigError, match="JOIN_CHANNEL_LINK is required"):
        load({**REQUIRED, "JOIN_CHANNEL": "-1001234"})


def test_log_level_is_checked_with_everything_else() -> None:
    assert load({**REQUIRED, "LOG_LEVEL": "debug"}).log_level == "DEBUG"
    with pytest.raises(ConfigError, match="LOG_LEVEL must be one of"):
        load({**REQUIRED, "LOG_LEVEL": "NOTALEVEL"})


def test_the_data_directory_is_private(tmp_path: Path) -> None:
    config = load({**REQUIRED, "DATA_DIR": str(tmp_path / "data")})
    old_umask = os.umask(0o022)
    try:
        secure_data_dir(config)
        Store(config.db_path).close()
        assert stat.S_IMODE(config.data_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(config.db_path.stat().st_mode) == 0o600
    finally:
        os.umask(old_umask)


def test_existing_files_are_made_private(tmp_path: Path) -> None:
    config = load({**REQUIRED, "DATA_DIR": str(tmp_path)})
    config.session_path.write_bytes(b"key")
    config.session_path.chmod(0o644)
    old_umask = os.umask(0o022)
    try:
        secure_data_dir(config)
    finally:
        os.umask(old_umask)
    assert stat.S_IMODE(config.session_path.stat().st_mode) == 0o600
