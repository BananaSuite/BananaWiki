# Getting started

This page gets a wiki running on your own computer in a few minutes and walks
through the first sign-in. For a server that other people use, continue with
[deployment](deployment.md) afterwards.

## Choose how to run it

| You want | Use |
|---|---|
| To try BananaWiki or develop it | [From source](#run-from-source) (below) |
| One wiki on a Linux server, with HTTPS, updates and backups handled for you | [`banana install --mode wiki`](deployment.md#managed-server-banana) |
| One wiki in containers | [Docker Compose](deployment.md#docker-and-docker-compose) |
| A wiki on a classroom or office computer, no server | [BananaWiki Desktop](desktop.md) |
| Wikis for other people (a hosting service) | [The hosting platform](hosting.md) |

## Run from source

You need Python 3.11 or newer and Git. The commands are for Linux and macOS;
on Windows use `.venv\Scripts\activate` instead of `. .venv/bin/activate`.

```sh
git clone https://github.com/BananaSuite/BananaWiki.git
cd BananaWiki
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
bananawiki serve
```

`bananawiki serve` starts a development server on `http://127.0.0.1:5001`
(`--port` changes the port; `--host` another address, which prints a warning;
`--debug` turns on the interactive debugger and only works on a loopback
address). Everything the wiki stores goes into `instance/` inside the
checkout. Stop it with Ctrl+C.

Without installing the package, `python -m pip install -r requirements.txt`
followed by `python -m bananawiki.cli serve` does the same from the checkout.

The development server is for trying things out. A real installation runs
Gunicorn behind an HTTPS proxy; see [deployment](deployment.md).

### Optional extras

* **Read aloud** needs Piper voices: `python -m pip install -e '.[tts]'` and
  ffmpeg from your system's packages for MP3 output. See [read aloud](tts.md).
  Without them the wiki works and the admin page says what is missing.
* **Development tools** (tests, linter): `python -m pip install -e '.[dev]'`.
  See [CONTRIBUTING](../CONTRIBUTING.md).

## First sign-in

1. Open `http://127.0.0.1:5001`. Until the first account exists every page
   leads to **/setup**.
2. The setup page asks for the **setup token**. It proves you are the person
   who installed the wiki. Print it in a second terminal (same folder, same
   virtual environment):

   ```sh
   bananawiki setup-token
   ```

   Keep it private until setup is done; afterwards it is no longer accepted.
   Paste it into the form (it is never accepted in the address bar).
3. Create your account: user name (3–50 letters, digits, `_` or `-`; names
   such as `admin`, `root` or `system` are reserved) and a password (8 to 1024
   characters). This first account is an **owner** and a
   **superuser**: nobody else can change it.
4. The **first-run wizard** follows. Choose the wiki's name, language and
   theme, the default set of features or your own selection (you can change
   every feature later under **Admin → Plugins**), whether new members see an introduction,
   optionally the first few accounts, and whether to add the built-in
   documentation as a category of pages.

You land on the home page. The sidebar lists the pages and categories; the
**Apps** menu holds the tools (kanban, canvases, chats …) that are switched on;
your account menu has **Settings**, your profile and **Admin**.

## What next

* Invite people: **Admin → Users** creates accounts, **Admin → Invite codes**
  creates codes for the sign-up page, and **Admin → Site settings** can open
  sign-up to everyone (optionally until a date) or require your approval.
* Decide who may do what: [permissions](permissions.md).
* Look through the [features](features.md) and switch off what you do not
  need.
* Read the [user guide](user-guide/en/README.md) (or the
  [guida utente](user-guide/it/README.md)) and share it with your members.
* Before real people depend on it: [deployment](deployment.md) and
  [operations](operations.md) (backups!).

## If something goes wrong

* **"The form expired or was sent from another site"**: the page was open too
  long or cookies are blocked. Reload and try again.
* **Lost the setup token**: run `bananawiki setup-token` again; it is derived
  from the secret key in `instance/.secret_key`.
* **Forgot your password**: `bananawiki reset-password <name>` (see
  [operations](operations.md#the-bananawiki-command)).
* **Port already in use**: `bananawiki serve --port 5002`.
* Anything else: the log is `instance/logs/bananawiki.log`, and
  `bananawiki config check` validates the configuration.
