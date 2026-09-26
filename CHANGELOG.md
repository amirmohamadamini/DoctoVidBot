# Changelog

## 2.0.0

A rewrite.

### Added

- **Videos are really made playable.** Each video is probed. A playable MP4 is
  sent back as it is. An MKV, AVI, WebM and similar is remuxed into MP4
  without re-encoding. A video that would need re-encoding is refused, with
  the reason.
- **A menu for every file**: make playable, send as file, rename (including the
  extension), and custom thumbnails.
- **A queue**, with a limit on files processed at once, each person's place in
  line shown, and a cancel button.
- **Buttons instead of commands.** A keyboard under the message box with
  ❓ Help and 📊 My usage, and an 🛠 Admin panel for admins. `/start` is the
  only command. Buttons are coloured: blue for the main action, green to
  confirm, red to cancel.
- **An optional cache channel**, turned on and off from the admin panel.
  A file sent again is answered from the channel instead of processed again.
- **Daily and monthly limits** per person, on both files and data, with
  higher limits for admins. 📊 My usage shows where someone stands.
- **A required channel** users must join, which admins turn on and off from
  the admin panel.
- **Broadcast**: an admin sends any message from the admin panel, as a copy,
  to everyone who has used the bot.
- **Backups**: the database, zipped and sent to every admin every 12 hours,
  or on demand from the admin panel.
- A disk check before each download, counting the space running jobs will
  still need, so parallel jobs can't fill the disk between them.
- Progress bars for downloading, repacking and uploading.
- Tests, type checking, linting and CI.

### Security

- **ffmpeg runs in its own container**, as its own user, with no network, no
  secrets and no access to the bot's data. The bot asks it for work over a
  socket on a shared scratch volume, and reads back its results without
  following links it might have planted.
- ffmpeg and ffprobe run without the bot's secrets in their environment, with
  memory, file size and output limits.
- Custom thumbnails are limited in size, pixels, time and concurrency.
- The data directory and its files are private to the bot's user.
- The container runs read-only, with no capabilities, no new privileges and
  resource limits.
- The cache channel only keeps videos made playable, never files sent back as
  they are.
- Dependabot keeps dependencies, the base image and CI actions up to date.

### Fixed

- Renaming didn't work: Telethon's `send_file` ignores `file_name`. Files are
  now uploaded under the chosen name.
- The video file itself was passed as its own thumbnail. A frame from the video
  is used now, or the user's picture.
- A file name containing `../` could write outside the temporary directory.
- Error details were shown to users. They go to the log now.

### Removed

- Every converted file was forwarded to a hard-coded account. The optional
  cache channel replaces this. It is off by default, and users are told when
  it's on.
- The sender's id is no longer put in the caption.

### Changed

- Python 3.14, Telethon 1.45. The Docker image uses prebuilt wheels and
  includes ffmpeg, runs as a non-root user, and keeps its session in a volume.
