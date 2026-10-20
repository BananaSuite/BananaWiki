<img src="bananawiki/wiki/static/favicons/apple-touch-icon.png" alt="Logo di BananaWiki" width="90" height="90">

# BananaWiki

BananaWiki è una wiki self-hosted per gruppi di lavoro, scuole e comunità:
pagine in Markdown con cronologia, permessi dettagliati e strumenti di
collaborazione facoltativi (chat, bacheche kanban, canvas, quiz, lettura ad
alta voce). Funziona come wiki singola, come app desktop su un solo computer
oppure come piattaforma hosting che dà a ogni cliente una wiki separata.

[English](README.md) · [Schermate](https://bananawiki.com/index.it.html#screenshots) ·
[Documentazione (in inglese)](docs/README.md) ·
[Guida utente](docs/user-guide/it/README.md) · [Aggiornare](UPGRADING.md)

## Funzionalità

* **Pagine**: editor Markdown con barra degli strumenti, anteprima,
  caricamento di immagini con testo alternativo, dimensione, allineamento e
  ritaglio, tabelle, video e `@menzioni`; categorie riordinabili con il
  trascinamento e navigazione sequenziale; ricerca nel testo; rilevamento dei
  conflitti di modifica; cronologia con differenze, ripristino e attribuzione;
  bozze salvate automaticamente; allegati; esportazione in PDF e Markdown;
  importazione ed esportazione in blocco; un editor a blocchi per comporre
  pagine.
* **Controllo degli accessi**: ruoli (utente, editor, amministratore,
  proprietario), ruoli personalizzati, permessi per singolo utente, accesso in
  lettura e scrittura per categoria, modalità pubblica, codici d'invito,
  iscrizione aperta con scadenza, approvazione dei nuovi account, verifica in
  due passaggi, sospensioni, gestione delle sessioni, registro di controllo.
* **Governance dei contenuti**: protezione delle pagine, prenotazioni con
  quote, proposte di modifica con revisione, 48 ore di tempo prima che una
  pagina eliminata sparisca, eliminazioni programmate, ruoli temporanei.
* **Collaborazione**: messaggi diretti e chat di gruppo con regole di
  conservazione, bacheche kanban, canvas visuali, quiz collegati alle pagine,
  annunci, distintivi, classifica dei contributori, profili con campi
  personalizzati.
* **Lettura ad alta voce** delle pagine con voci neurali locali (Piper) o un
  server GPU.
* **Integrazione**: API REST con token e ambiti, federazione tra wiki, pagine
  personalizzate a qualsiasi indirizzo libero, plugin di terze parti
  (disattivati di default; solo il proprietario o un superutente può
  installarli; i plugin della 1.4 funzionano tramite un livello di
  compatibilità).
* **Amministrazione**: temi grafici, lingue dell'interfaccia (inglese,
  italiano e tedesco incluse, altre caricabili), guida utente integrata,
  esportazione e importazione dell'intero sito, eliminazione in blocco.
* **Operatività**: installazione con un comando e configurazione HTTPS,
  aggiornamenti con backup e ripristino automatico in caso di errore, backup
  cifrati in un repository Git privato, Docker Compose, app desktop per
  Windows, macOS e Linux.
* **Piattaforma hosting**: registrazione, una wiki per cliente in container
  isolati, domini personalizzati, collaboratori, quote e scadenze, accesso con
  l'account della piattaforma, API propria, backup su Google Drive. Ogni nuova
  wiki ottiene subito il certificato HTTPS, anche dietro Cloudflare, e le wiki
  che si sono fermate vengono riavviate.

## Per iniziare

**Dal codice sorgente** (Python 3.11 o successivo, Git):

```sh
git clone https://github.com/BananaSuite/BananaWiki.git
cd BananaWiki
python3 -m venv .venv && . .venv/bin/activate
python -m pip install -e .
bananawiki serve
```

Apri <http://127.0.0.1:5001>, esegui `bananawiki setup-token` in un secondo
terminale (stessa cartella, stesso ambiente virtuale) e inserisci il codice
per creare il primo account.

**Docker Compose** (una wiki dietro Caddy con HTTPS automatico; per costruire
l'immagine Docker deve avere il componente BuildKit, `docker buildx`):

```sh
git clone https://github.com/BananaSuite/BananaWiki.git
cd BananaWiki
WIKI_DOMAIN=wiki.example.org ACME_EMAIL=tu@example.org docker compose up -d
docker compose exec wiki python -m bananawiki.cli setup-token
```

**Server Linux gestito** (servizi systemd, HTTPS, aggiornamenti, backup).
Installa dall'ultimo tag di rilascio:

```sh
git clone https://github.com/BananaSuite/BananaWiki.git
cd BananaWiki
git checkout "$(git tag -l 'v*' | sort -V | tail -1)"
sudo ./banana install --mode wiki --domain wiki.example.org
sudo bananawiki proxy --install --email tu@example.org
sudo bananawiki setup-token
```

Con `--mode hosting` si installa la piattaforma hosting (serve Docker con
BuildKit). Va bene qualsiasi filesystem; su XFS montato con `prjquota` ogni
wiki ha anche un limite di spazio rigido. Gli aggiornamenti automatici
restano disattivati finché non esegui
`sudo bananawiki updates enable --require-signatures ALLOWED_SIGNERS`.

**Desktop**: [BananaWiki Desktop](docs/desktop.md) fa funzionare una wiki su
un computer di classe o d'ufficio, senza server.

Per aggiornare dalla 1.6 o dalla 1.4, leggi prima [UPGRADING.md](UPGRADING.md)
(in inglese).

## Sviluppo

```sh
python -m pip install -e ".[dev]"
python -m pytest tests -q -n 2
python -m ruff check .
```

Usa `python -m pytest`, non `pytest`: i test si importano a vicenda come
pacchetto. La CI esegue i test con Python dalla 3.11 alla 3.14.

## Documentazione

La [documentazione](docs/README.md) è in inglese; la
[guida utente](docs/user-guide/it/README.md) anche in italiano. Chi vuole
contribuire parte da [CONTRIBUTING.md](CONTRIBUTING.md) e
[ARCHITECTURE.md](ARCHITECTURE.md) e segue il
[codice di condotta](CODE_OF_CONDUCT.md). Le segnalazioni di sicurezza
seguono [SECURITY.md](SECURITY.md), non le issue pubbliche.

## Licenza

BananaWiki è distribuita con la GNU Affero General Public License versione 3
(`AGPL-3.0-only`); vedi [LICENSE](LICENSE) e [NOTICE](NOTICE). L'uso
personale e commerciale è consentito.

Se modifichi BananaWiki e la rendi usabile in rete, la sezione 13 della AGPL
ti chiede di offrire agli utenti il codice sorgente corrispondente. Imposta
`BW_SOURCE_URL` sul sorgente della versione che usi: la wiki lo collega
all'indirizzo `/source`. Conserva le note di terze parti.

Avviata nel febbraio 2026 da Luca Zani
([OverloadedTech](https://github.com/OverloadedTech)); vedi [NOTICE](NOTICE).

Copyright © 2026 Luca Zani e collaboratori. Ogni collaboratore conserva il
copyright dei propri contributi.
