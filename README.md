# Self-hosted Telegram Bot - TinyASR

The bot runs continuously on a Linux VPS with Docker Compose and uses the Telegram Bot API with **long polling**. Send a YouTube video URL to compare compatible subtitles with TinyASR, or upload a Telegram video, voice message, or audio file to receive its TinyASR transcription. TinyASR automatically detects the spoken language. Downloads and transcription run on the VPS.

## Start the bot on a Linux VPS

You need a Linux VPS, a Telegram bot created with BotFather, Docker Engine, and the Docker Compose plugin. Install Docker for your Linux distribution using the [official instructions](https://docs.docker.com/engine/install/), then verify the installation:

```sh
docker --version
docker compose version
```

Connect to the VPS over SSH, clone the repository, and enter its directory:

```sh
ssh username@your-vps-ip
git clone https://github.com/mopitithomne/compare-video-subtitles-with-tinyasr.git
cd compare-video-subtitles-with-tinyasr
```

Create the environment file and enter the token provided by BotFather:

```sh
cp .env.example .env
nano .env
```

Replace the value of `TELEGRAM_BOT_TOKEN` with your bot token and save the file. Restrict access to the file:

```sh
chmod 600 .env
```

Set `BOT_OWNER` to the owner's numeric Telegram user ID. The owner must start a chat with the bot before receiving report and access-request notifications. The owner is automatically authorized; other users must be approved from the owner's notification before they can use the bot.

Build and start the bot in the background:

```sh
sudo docker compose up --build -d
sudo docker compose ps
sudo docker compose logs -f telegram-asr-bot
```

If Docker is configured to run without `sudo` for your user, omit `sudo`. The log command stays open; press `Ctrl+C` to exit the log view without stopping the bot. Open a chat with your bot in Telegram and send `/start`.

The VPS does not need an incoming port open for Telegram. The bot makes outbound connections to the Telegram API. Only one process can long-poll with a bot token at a time, so stop any other instance using the same token before starting this one.

## Manage the bot

View the logs:

```sh
sudo docker compose logs -f telegram-asr-bot
```

After making code changes, rebuild and recreate the bot, then follow its latest 100 log lines:

```sh
sudo ./restart-bot.sh
```

If Docker runs without `sudo` for your user, use `./restart-bot.sh` instead. Press `Ctrl+C` to stop following the logs; the bot keeps running.

Stop the bot:

```sh
sudo docker compose down
```

The SQLite database and TinyASR Chromium profile remain in the Docker volume `bot-data` after the container stops or is updated. To delete this data as well, `docker compose down -v` permanently removes the volume.

## How it works

- The bot uses Telegram's `getUpdates` long-polling API and saves the latest processed update ID in SQLite.
- At startup, it removes any webhook configured for the bot token, then starts polling.
- Jobs and their state are stored in SQLite. A job interrupted by a restart is resumed on the next startup.
- New users are told that owner approval is required. The owner receives their name, username (if set), Telegram ID, and an approval button; after approval, the user receives a confirmation and can use the bot.
- Users can upload a Telegram video, voice message, or audio file to receive its TinyASR transcription as text messages. TinyASR automatically detects the spoken language; FFmpeg prepares the audio, and no YouTube subtitles are required. For uploaded media and YouTube comparisons, interim TinyASR text streams in private chats using Telegram's ephemeral message drafts. Telegram's hosted Bot API download limit restricts uploads to 20 MB; uploads are also limited to 60 minutes.
- Completed reports and their audio, subtitle, transcript, HTML, and alignment files are saved in the `bot-data` volume. New reports notify the configured owner with a button to retrieve them.
- If a video already has a saved report, the bot sends it immediately and offers a button to run a fresh comparison. This reuse is global across users. `/reports` lists reports available to the requesting user; tap a report to receive its audio and HTML again.
- The bot processes one video job at a time, with up to three additional users waiting. It reports an estimated queue position when a request is admitted. Each user can have only one queued or processing job. A new link silently replaces that user's job until audio processing starts; after that, new links are rejected until it finishes.
- Only subtitle tracks matching a language available in the video's audio tracks are offered. Manual subtitles are preferred over automatic subtitles within those languages. If the audio language cannot be verified or no matching subtitle track exists, the bot will not compare mismatched languages.
- When automatic subtitles are used, the final result warns that they may be inaccurate. WebVTT timestamps and repeated cues are cleaned before comparison.
- Videos are limited to 60 minutes and downloaded audio files to 500 MB. Temporary working files are deleted after the result files have been sent.
- At the end of each successful job, the bot sends only the playable audio followed immediately by the HTML difference report. The HTML has a button to reveal the full comparison table, metrics, source subtitles, and TinyASR transcript in Telegram.
- The HTML report shows all YouTube subtitle text beside the full TinyASR transcript, highlighting differing source words in red and differing or additional TinyASR words in green. Its differing-cue count is a cue-level measure, not a word-level match percentage. Telegram's rich table uses the `marked` formatting on differing words in both columns; Telegram does not support custom red/green text colors for rich messages.
- Difference timestamps use inline word timing when available in WebVTT subtitles; otherwise, they use the start of the corresponding subtitle cue and may be approximate.
- Audio is sent through Telegram's audio player as MP3. Non-MP3 audio and MP3 files larger than 45 MB are converted to mono 64 kbps MP3; the original downloaded audio is still used for transcription.
- **WER (Word Error Rate)** measures word-level transcription errors: word substitutions, deletions, and insertions divided by the number of words in the reference subtitles.
- **CER (Character Error Rate)** uses the same calculation at the character level, divided by the number of characters in the reference subtitles.
- Lower WER and CER scores mean the transcription is closer to the subtitles. A score of 0% means an exact match.

## Data and secrets

- The Telegram bot token is stored in `.env`, which Git ignores. Do not share it.
- The SQLite database and Chromium profile are stored in the `bot-data` Docker volume.
- Saved report files are also stored in the `bot-data` Docker volume and remain available after restarts.
- Deno, FFmpeg, and Chromium are installed automatically in the Docker image.
- The deployment does not use Google Cloud services.

## Troubleshooting YouTube sign-in errors

If the logs show `Sign in to confirm you're not a bot`, YouTube is rejecting requests from the VPS. First rebuild the image without using Docker's cached dependency layer, then restart the bot:

```sh
sudo docker compose build --pull --no-cache
sudo docker compose up -d
```

If the error persists, you can optionally provide a Netscape-format cookies file from a browser where you are signed in to YouTube and authorized to access the video. On Windows, install Firefox, sign in to YouTube, close Firefox, then run this in PowerShell from the folder containing `yt-dlp.exe`:

```powershell
.\yt-dlp.exe --cookies-from-browser firefox --cookies youtube-cookies.txt
```

yt-dlp may print `You must provide at least one URL` after `Extracted ... cookies from firefox`. The export may still have succeeded; check that the file exists and is not empty:

```powershell
Test-Path .\youtube-cookies.txt
(Get-Item .\youtube-cookies.txt).Length
```

If the file exists and has a nonzero size, use it as the cookies file.

This exports cookies for all sites, not just YouTube. Treat the file like a password: use a dedicated browser profile if possible, never commit or share the file, and transfer it securely to the VPS. From the project directory on the VPS, create the ignored secrets directory:

```sh
mkdir -p secrets
chmod 700 secrets
```

From your computer, copy the file to the VPS (the command assumes you cloned the repository into your home directory; adjust the path if needed):

```sh
scp youtube-cookies.txt username@your-vps-ip:~/compare-video-subtitles-with-tinyasr/secrets/youtube-cookies.txt
```

Then, on the VPS, restrict access and configure the container path in `.env`:

```sh
chmod 600 secrets/youtube-cookies.txt
nano .env
```

Because the file was exported on Windows, convert its line endings for Linux:

```sh
sudo apt-get install -y dos2unix
dos2unix secrets/youtube-cookies.txt
```

Set `YTDLP_COOKIES_FILE=/app/secrets/youtube-cookies.txt`, save the file, and recreate the container:

```sh
sudo docker compose up -d --force-recreate
```

The `secrets/` directory is mounted read-write because yt-dlp saves updated cookies back to the file, and it is ignored by Git. If the logs still show `Sign in to confirm you're not a bot`, verify that `YTDLP_COOKIES_FILE` is set in `.env` and points to this file. Cookies grant access to your account and may expire; never send them in chat or logs. Cookies may not resolve a block on the VPS's IP address. If YouTube continues to reject requests, use an authorized access method or contact the VPS provider rather than sharing account cookies.

## Tests

Run the tests from this directory after installing Python and the project dependencies:

```sh
python3 -m unittest discover -s tests -v
```
