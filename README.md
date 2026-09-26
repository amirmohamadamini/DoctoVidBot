# DoctoVid

A Telegram bot that turns a video sent as a file into a video you can play
right in the chat. It can also rename any file, change its extension, or give
it a new thumbnail.

Try it: [@DoctoVidbot](https://t.me/DoctoVidbot)

## What it does

Send the bot a file and it answers with a menu:

| Button | What happens |
|---|---|
| **▶️ Make playable** | For videos. Comes back as a streamable video with a duration, a scrubber and a thumbnail. |
| **📄 Send as file** | The file comes back untouched, with any new name or thumbnail. Works for any file. |
| **✏️ Rename** | Type a new name. Add an extension to change it too. |
| **🖼 Thumbnail** | Send a photo to use as the cover. |

When several people send files at once, the extra files wait in a queue and
each person sees their place in it, with a button to cancel.

Everything is done with buttons. `/start` puts a keyboard under the message
box with **❓ Help** and **📊 My usage**, plus **🛠 Admin panel** for admins.
Buttons are coloured by what they do: the main action is blue, confirming is
green, and cancelling is red. Telegram apps too old for colours show them as
ordinary buttons.

Each person has a daily and a monthly limit, on both the number of files and
their total size. The **📊 My usage** button shows how much they've used.

## How "make playable" works

Telegram only plays MP4 inside the chat. What it does with a video depends on
the container, not on how the file is uploaded: an MKV is shown as a file to
download however it is sent. So the bot probes each video with `ffprobe` and
does the least it can:

1. **Already a playable MP4** (H.264, HEVC, AV1 or MPEG-4 video, with AAC or
   MP3 audio). It is sent straight back as a streamable video. ffmpeg isn't
   run at all.
2. **Playable streams in the wrong container**, such as an MKV, AVI or WebM
   holding H.264. The streams are copied into an MP4 with `-c:v copy`, so
   the video isn't re-encoded and the quality stays the same. This takes
   seconds, not minutes. Audio MP4 can't carry, such as AC-3 or DTS, is
   converted to AAC, so the video doesn't play silently. Subtitles are dropped.
3. **Video that would need re-encoding**, such as VP9. The bot says so and
   doesn't touch it. The file can still be renamed or given a thumbnail.

## Running it

You need Docker, and four things from Telegram:

- `API_ID` and `API_HASH` from <https://my.telegram.org/apps>;
- `BOT_TOKEN` from [@BotFather](https://t.me/BotFather);
- your own numeric user id for `ADMIN_IDS`, for example from
  [@userinfobot](https://t.me/userinfobot).

```bash
git clone https://github.com/amirmohamadamini/DoctoVidBot.git
cd DoctoVidBot
cp .env.example .env    # then fill it in
docker compose up -d --build
docker compose logs -f
```

This starts two services, the bot and the media worker that runs ffmpeg for it
(see [Security](#security)). If the build fails with a "TLS handshake timeout" from Docker Hub, set
`PYTHON_IMAGE` in `.env` to one of the mirrors listed in `.env.example`; it is
the same official image. The Telegram session and the bot's small database
live in the `data` volume, so rebuilding the image doesn't log the bot in
again.

### Settings

All settings are in `.env`. [`.env.example`](.env.example) explains each one.

| Setting | Default | |
|---|---|---|
| `API_ID`, `API_HASH`, `BOT_TOKEN` | — | Required |
| `ADMIN_IDS` | — | Who may use the admin commands |
| `CACHE_CHANNEL_ID` | — | See [Cache channel](#cache-channel) |
| `MAX_FILE_MB` | 2000 | Largest file accepted. 2000 is Telegram's limit for bots |
| `WORKERS` | 2 | Files processed at the same time |
| `MAX_QUEUE` | 50 | Most files waiting before new ones are turned away |
| `JOBS_PER_USER` | 3 | Most files one person may have queued or in progress |
| `MENU_TIMEOUT_MINUTES` | 15 | How long an unanswered menu stays open |
| `USER_DAILY_FILES`, `USER_MONTHLY_FILES` | 20, 300 | Files per person, per day and per month |
| `USER_DAILY_GB`, `USER_MONTHLY_GB` | 10, 100 | Data per person, per day and per month |
| `ADMIN_DAILY_FILES`, `ADMIN_MONTHLY_FILES` | 100, 2000 | The same, for admins |
| `ADMIN_DAILY_GB`, `ADMIN_MONTHLY_GB` | 50, 500 | |
| `TIMEZONE` | UTC | When days and months start, for the limits |
| `JOIN_CHANNEL`, `JOIN_CHANNEL_LINK` | — | See [Required channel](#required-channel) |
| `BACKUP_INTERVAL_HOURS` | 12 | See [Backups](#backups) |

Any limit set to 0 is turned off. The limits count files that were delivered,
including files served from the cache, by the size of the file the person
sent. A failed or cancelled file costs nothing. Files still waiting in the queue count too, so nobody can queue past
their limit.

A setting with a bad value stops the bot at startup, with a list of every
problem found.

### Cache channel

When the cache is on, every video the bot makes playable is also copied to a
private channel. The next time anyone sends the same Telegram file, the bot
sends that copy straight back instead of downloading and repacking it again.

Only **▶️ Make playable** results are cached, and only when they weren't
renamed or given a thumbnail. Files sent back with **📄 Send as file** never
are: they're most often someone's own documents, and sending them again costs
the bot almost nothing anyway. Anyone who can see the channel can see what's in
it, so keep it private and its admins few.

1. Create a private channel and add the bot as an admin that can post
   messages.
2. Put the channel's id, which starts with `-100`, in `CACHE_CHANNEL_ID` and
   restart.
3. Post any message in the channel. A bot only learns a channel from an update,
   so one it was added to before it started doesn't know it yet.
4. In the 🛠 Admin panel, tap **🗄 Cache: off** to turn it on. The bot posts
   and deletes a test message first, to check it can.

Tap it again to stop using the channel. Files already there stay there.

The cache is off by default. When it's on, `/start` tells users that finished
files are kept.

### Required channel

You can make people join a channel before they use the bot.

1. Add the bot to the channel as an admin. It needs this to see who is a
   member.
2. Set `JOIN_CHANNEL` to the channel's `@username`. For a private channel, use
   its id (starting `-100`) and put its invite link in `JOIN_CHANNEL_LINK`.
   Then restart.
3. Post any message in the channel, so the bot has seen it.
4. In the 🛠 Admin panel, tap **🔒 Join required: off** to turn it on. The bot
   checks it can see the members first.

Anyone who isn't a member gets a button to join and an "I've joined" button.
Admins never have to join. If the bot can't check (for example, it stopped
being an admin of the channel), it lets people in rather than locking everyone
out, and logs a warning. Tap the button again to turn the rule off.

### Broadcast

Tap **📣 Broadcast** in the admin panel, then send the message you want to
send: text, a photo, a file, anything. The bot says how many people it will
reach and waits for you to tap **Send**. It sends the message to everyone who has used
the bot, as a copy, so it doesn't show who wrote it. It reports its progress as
it goes.

People who have blocked the bot are skipped from then on, until they use it
again. Sending is paced to stay under Telegram's limits, so a large broadcast
takes a while.

### Backups

Every `BACKUP_INTERVAL_HOURS` (12 by default), each admin is sent
`doctovid-backup-<date>.zip`. It holds the database: users, usage, the cache
index and the admin settings. **💾 Backup now** in the admin panel sends one
straight away. The schedule
is counted from the last backup, so restarting the bot doesn't skip one or
send an extra.

The Telegram session file is never included. Anyone holding it could act as
your bot.

An admin only receives backups after they have sent the bot a message at least
once, because Telegram doesn't let bots message people first.

To restore, unzip the backup next to `compose.yaml`, then copy `doctovid.db`
into the data volume while the bot is stopped. Doing the copy from the bot's
own image means the file belongs to the user the bot runs as:

```bash
docker compose stop bot
docker compose run --rm -v "$PWD/doctovid.db:/restore.db:ro" bot cp /restore.db /data/doctovid.db
docker compose start bot
```

### Admin panel

Admins get a **🛠 Admin panel** button on their keyboard. It opens one
message that shows jobs running and waiting, users, cached files and the
backup schedule, with these buttons:

| Button | |
|---|---|
| 🗄 Cache: on/off | Use the cache channel, or stop |
| 🔒 Join required: on/off | Make users join the channel first, or stop |
| 📣 Broadcast | Send a message to everyone |
| 💾 Backup now | Send a backup to the admins |
| 🔄 Refresh | Update the numbers |

The admin buttons only work for the ids in `ADMIN_IDS`, even if someone else
gets hold of a panel message.

## Limits

- Files up to 2 GB, because that is what Telegram lets bots upload.
- The queue is held in memory. After a restart, files that were waiting have
  to be sent again.
- Videos that need re-encoding aren't converted. That would take minutes of CPU
  per minute of video.

## Security

The bot handles files from strangers, and the risky part is ffmpeg, which has
to parse them. So ffmpeg doesn't run in the bot at all.

### Two containers

`docker compose` starts two services from the same image:

| | `bot` | `media` |
|---|---|---|
| Runs | the bot (`doctovid`) | ffmpeg and ffprobe (`doctovid-media`) |
| User | `doctovid` (10001) | `media` (10002) |
| Network | yes, to reach Telegram | **none** |
| Secrets | `BOT_TOKEN`, `API_HASH`, the session | **none** |
| Sees `/data` | yes | **no** |
| Sees `/scratch` | yes | yes |

They share only the `scratch` volume. The bot downloads each file into a job
folder there and asks the worker, over a Unix socket on the same volume, to
probe it, repack it or make a thumbnail. If a crafted file took over ffmpeg,
it would find itself as an unprivileged user in a container with no network,
no secrets and no way into `/data`.

The bot doesn't trust what comes back:
- It opens every result without following symlinks, and only if it's a plain
  file, then uploads from that same open file. A planted link to the session
  file is refused.
- Job folders have random names. The worker can enter a job's folder but can't
  list or create them, so it can't reach other jobs' files by guessing.
- The worker refuses any path outside the scratch folder.
- Finished custom thumbnails are copied out of scratch into a folder only the
  bot can read.

### And inside each container

- **ffmpeg runs with as little as possible.** Its environment holds only
  `PATH`. Each run gets 1 GB of memory, two threads and a time limit, can't
  write a file much bigger than the job needs, and is the first thing the
  kernel kills if memory runs out. What ffprobe prints is capped too.
- **Both processes are non-dumpable**, so nothing else running as their user
  can read their environment or memory through `/proc`.
- **Files are kept under names the bot chooses.** The name a user gave a file
  is cleaned, and only ever used as the name on the file sent back.
- **Custom thumbnails** are limited to 10 MB and 40 megapixels, time-limited,
  processed at most two at a time, and limited to 10 attempts per person per
  hour.
- **The data directory is private** to the bot's user (`0700`, files `0600`).
  The Telegram session in it is as good as the bot's password; it's never
  included in backups.
- **Both containers** drop every Linux capability, can't gain new privileges,
  have a read-only filesystem apart from their volumes and a small `/tmp`, and
  have memory, CPU and process limits (see `compose.yaml`).
- **Admin buttons** check the user id on every press. Callback data never
  carries a path or a file name.

### What this doesn't cover

- **A compromised worker can see other jobs' files while it stays
  compromised.** It still can't reach the bot's session, the database, the
  network or Telegram. Restarting the `media` service clears anything left
  running in it.
- **Running without Docker** (`uv run doctovid`) runs ffmpeg inside the bot
  process, and the bot logs a warning saying so. That's fine for trying the bot
  out, not for serving strangers.

ffmpeg's security fixes arrive through the base image, so rebuild regularly
(see below). Dependabot opens a pull request each week when a dependency, the
base image or a CI action has a newer version.

## Updating

```bash
git pull
docker compose build --pull
docker compose up -d
```

`--pull` fetches the newest base image, which is how ffmpeg and the operating
system get their security fixes.

## Development

Needs Python 3.14, [uv](https://docs.astral.sh/uv/) and ffmpeg.

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

Tests run in parallel on every core, through pytest-xdist. Add `-n0` to run them
in one process, for example under a debugger.

To run it locally, put a `.env` in the project root and run `uv run doctovid`.
ffmpeg then runs in-process. To try the two-process setup without Docker, run
`uv run doctovid-media` with `MEDIA_SOCKET` and `SCRATCH_DIR` set, and give
the bot the same two settings.

The code is small, and each module has one job:

| Module | |
|---|---|
| `bot.py` | Telegram handlers: files in, menus out, buttons into jobs |
| `jobs.py` | Requests, jobs, and the queue |
| `runner.py` | One job from download to delivery, plus the cache |
| `media.py` | ffprobe and ffmpeg: what a video needs, the remux, thumbnails |
| `worker.py` | The media worker: `media.py` behind a socket, in its own container |
| `tools.py` | How the bot reaches ffmpeg: through the worker, or in-process |
| `safefile.py` | Opening files the worker could have tampered with |
| `hardening.py` | Process-level protection shared by both |
| `transport.py` | The Telegram calls the runner makes, behind an interface tests can fake |
| `admin.py` | The admin panel |
| `quota.py` | Daily and monthly limits |
| `membership.py` | The required-channel check |
| `broadcast.py` | Sending a message to every user |
| `backup.py` | Zipped database backups for the admins |
| `menus.py` | What the bot says, and its buttons |
| `names.py` | Cleaning file names from users |
| `progress.py` | Progress bars, and throttling edits |
| `store.py` | SQLite: admin settings, users, usage, the cache index |
| `config.py` | Settings from the environment |

## License

[GPL-3.0](LICENSE)
