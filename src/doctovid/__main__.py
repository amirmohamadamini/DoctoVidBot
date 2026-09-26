"""Entry point: `doctovid`, or `python -m doctovid`."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import sys

from dotenv import load_dotenv
from telethon import TelegramClient

from . import __version__
from .bot import Bot
from .config import Config, ConfigError, load
from .hardening import make_undumpable
from .store import Store
from .tools import tools_for

log = logging.getLogger("doctovid")


def secure_data_dir(config: Config) -> None:
    """Only the bot's own user may read what it keeps.

    The session file is as good as the bot's password, and the database lists
    everyone who has used it. Everything created from here on is private too.
    """
    os.umask(0o077)
    config.data_dir.mkdir(parents=True, exist_ok=True)
    config.data_dir.chmod(0o700)
    for path in (config.session_path, config.db_path):
        if path.exists():
            path.chmod(0o600)


def prepare_work_dirs(config: Config) -> None:
    """Empty the scratch, thumbnail and backup directories.

    Anything in them belongs to a job that died with the last process, and
    nothing can resume it. The scratch directory itself is kept, not deleted
    and recreated: in the container it is a shared volume whose permissions
    were set up for the media worker, and only its contents are the bot's.
    """
    for directory in (config.work_dir, config.thumbnail_dir, config.backup_dir):
        directory.mkdir(parents=True, exist_ok=True)
        for entry in directory.iterdir():
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)


async def run(config: Config) -> None:
    secure_data_dir(config)
    prepare_work_dirs(config)
    tools = tools_for(config.media_socket)

    store = Store(config.db_path)
    client = TelegramClient(str(config.session_path), config.api_id, config.api_hash)
    client.parse_mode = "html"
    await client.start(bot_token=config.bot_token)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.ensure_future(client.disconnect()))

    bot = Bot(client, config, store, tools=tools)
    bot.start()
    await bot.publish_commands()
    await bot.check_channels()
    me = await client.get_me()
    log.info("doctovid %s running as @%s", __version__, me.username)
    try:
        await client.run_until_disconnected()
    finally:
        await bot.stop()
        store.close()
        log.info("stopped")


def main() -> None:
    load_dotenv()
    try:
        config = load()
    except ConfigError as exc:
        print("doctovid can't start. Fix these settings:", file=sys.stderr)
        for problem in exc.problems:
            print(f"  - {problem}", file=sys.stderr)
        sys.exit(2)

    logging.basicConfig(
        level=config.log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("telethon").setLevel(max(logging.WARNING, logging.root.level))
    if not make_undumpable():
        log.warning("could not make the process undumpable; ffmpeg could read its memory")
    asyncio.run(run(config))


if __name__ == "__main__":
    main()
