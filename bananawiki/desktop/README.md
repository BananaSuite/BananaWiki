# BananaWiki Desktop

A one-window launcher that runs a BananaWiki on an ordinary Windows, macOS or
Linux computer, for a teacher or anyone who does not want to administer a
server. It replaces the 1.4 "Easy Deployment App" and opens its data folders
in place.

## Using it

1. Open **BananaWiki** (`BananaWiki.exe`, `BananaWiki.app` or `BananaWiki`).
2. Check the **data folder**. Everything the wiki stores lives there; keep it
   and you keep the wiki. **Change…** picks another one (an empty folder or an
   existing BananaWiki data folder; for a folder with other files the launcher
   offers a new `BananaWiki` folder inside it).
3. Optional: tick **Let other devices on this network open the wiki**. The
   launcher explains what this means and asks for confirmation first. Without
   it the wiki only listens on this computer (`127.0.0.1`).
4. Click **Start**. The browser opens the wiki; the window shows the address
   for this computer and, when sharing, the address and a QR code for phones,
   tablets and pupils' computers.
5. First start only: the setup page asks for a **setup code**. The window
   shows it with a **Copy** button until the administrator account exists.
6. Click **Stop** (or close the window) when done.

The launcher speaks English and Italian (menu in the top-right corner). It
remembers the folder, language, sharing choice and port in the user's
configuration folder (`%APPDATA%\BananaWiki`, `~/Library/Application
Support/BananaWiki` or `~/.config/bananawiki`).

No internet connection is needed. For other devices to connect, they must be
on the same local network (classroom router, hotspot). The operating system
may ask to allow BananaWiki through the firewall. The wiki uses port 80 when
it can (short addresses), otherwise 8080 or a port from 8000 to 8099.

## The data folder

```
<data folder>/
  .portable-data.json     marks the folder as BananaWiki's
  instance/bananawiki.db  database
  instance/.secret_key    signs sessions and derives the setup code
  instance/translations/  uploaded languages
  uploads/ attachments/ chat_attachments/ kanban_attachments/
  custom_page_files/ favicons/ tts/ plugins/
  logs/bananawiki.log     look here when something goes wrong
  previous/<time>/        data replaced by a restore
```

A packaged app keeps its data beside itself by default (`bananawiki/` next to
the executable or the `.app`, as 1.4 did), which makes it portable on a USB
stick; when that place is not writable it uses `Documents/BananaWiki`.

Only one launcher can serve a folder at a time (a lock file,
`.bananawiki.lock`, guards it).

## Backups

* **Back up…** writes one ZIP file: a consistent copy of the database (SQLite
  online backup, so it works while the wiki is running), the secret key,
  uploaded languages and every uploaded file. Logs are not included.
* **Restore…** (wiki stopped) checks the whole archive first (safe file names
  only, no links, sizes, an intact BananaWiki database not newer than this
  version), unpacks it beside the data and then swaps it in. The replaced data
  is kept under `previous/<time>/`. If the computer stops half-way, the next
  start puts the old data back. An archive without a key keeps the current
  key.
* **Delete all data…** (wiki stopped, after confirmation) removes pages,
  accounts and files but keeps the key.

The ZIP layout is the one the 1.4 launcher's "Export All" wrote (plus
`instance/translations/`), so 1.4 exports restore here.

## Command line

The same program (or `python -m bananawiki.desktop` from a source install)
has headless commands:

```
BananaWiki serve   --data-dir DIR [--port N] [--lan] [--language it] [--backend waitress|gunicorn]
BananaWiki backup  --data-dir DIR --output FILE.zip
BananaWiki restore --data-dir DIR FILE.zip
```

`serve` listens on this computer only unless `--lan` is given, prints the
addresses (and the setup code while setup is pending) and stops on Ctrl+C.

## How it works

* `datafolder.py` validates the folder, creates the layout, holds the lock and
  generates the secret key file. It builds the `BW_*` environment from scratch
  (nothing is inherited from the user's shell, so a stray `SECRET_KEY` cannot
  change the key) with `BW_EASY_DEPLOYMENT=1` and `BW_ENV=production`.
* `server.py` serves `bananawiki.wiki.app.create_app` with **waitress** on a
  background thread of the launcher (every platform; always bundled). From a
  source install on Linux or macOS without waitress it runs **gunicorn** with
  one worker as a child process instead. The server is ready when `/health`
  answers with the nonce drawn for this start (response header
  `X-BananaWiki-Start`); an answer without it comes from another program on
  the same port and stops the start with an error.
* `backup.py` writes and restores the ZIP files.
* `network.py` finds the LAN address without contacting the internet and
  picks a free port. The launcher binds waitress's port itself: on Windows
  for exclusive use (`SO_EXCLUSIVEADDRUSE`), so no other program can bind the
  same port and receive the wiki's requests; on Linux and macOS with
  `SO_REUSEADDR`, as waitress does.
* `app.py` is the tkinter window; slow work runs on a worker thread.
* Strings live in `translations/en.json` and `translations/it.json`.

## Building

PyInstaller does not cross-compile: build on each system.

```
python -m pip install . waitress pyinstaller
python packaging/desktop/build.py
python packaging/desktop/smoke_test.py packaging/desktop/dist/BananaWiki-Desktop-<os>-<arch>.<zip|tar.gz>
```

`packaging/desktop/` holds the PyInstaller spec (bundle id
`org.bananawiki.desktop`), the icons, the build script (which wraps the output
in an archive that keeps executable bits) and the smoke test (runs `serve`,
checks `/health` and `/setup`, then `backup`). The GitHub workflow
`.github/workflows/desktop-app.yml` runs the tests, the build and the smoke
test on Windows, Linux and macOS for changes to the launcher.

The builds contain the wiki and the launcher only: no hosting portal, no
server lifecycle tools and no Piper text-to-speech (install BananaWiki
normally for read-aloud).

## Differences from the 1.4 Easy Deployment App

* Network sharing is off until the user ticks it and confirms; 1.4 shared on
  the LAN by default.
* The first-run setup code is shown in the window (the wiki no longer accepts
  it in the URL).
* The user chooses the data folder; folders with unrelated files are refused.
* Backups can be taken while the wiki runs; restores keep the replaced data.
* The server runs in the launcher process (waitress) instead of a hidden
  child copy of the executable, and the `[smoke] STEP` debug prints and
  diagnostic marker files are gone.
* The internet check, Wi-Fi name display and flag pictures were dropped.
