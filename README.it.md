<img src="bananawiki/wiki/static/favicons/apple-touch-icon.png" alt="Logo di BananaWiki" width="90" height="90">

# BananaWiki

BananaWiki è una wiki self-hosted per gruppi di lavoro, scuole e comunità.
Offre pagine in Markdown con cronologia, permessi dettagliati e strumenti di
collaborazione facoltativi come chat, bacheche kanban, canvas, quiz e lettura
ad alta voce. Puoi usarla come wiki singola, come app desktop su un solo
computer oppure come piattaforma di hosting che dà a ogni cliente la propria
wiki.

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
  annunci, distintivi, una classifica dei contributori e profili con campi
  personalizzati.
* **Lettura ad alta voce** delle pagine con voci neurali locali (Piper) o un
  server GPU.
* **Integrazione**: API REST con token e ambiti, federazione tra wiki, pagine
  personalizzate a qualsiasi indirizzo libero e plugin di terze parti. I
  plugin sono disattivati di default e solo il proprietario o un superutente
  può installarli. I plugin scritti per la 1.4 funzionano ancora tramite un
  livello di compatibilità.
* **Amministrazione**: temi grafici, lingue dell'interfaccia (inglese,
  italiano e tedesco sono incluse e puoi caricarne altre), guida utente
  integrata, esportazione e importazione dell'intero sito ed eliminazione in
  blocco.
* **Operatività**: installazione con un comando e configurazione HTTPS,
  aggiornamenti con backup e ripristino automatico in caso di errore, backup
  cifrati in un repository Git privato, Docker Compose, app desktop per
  Windows, macOS e Linux.
* **Piattaforma di hosting**: registrazione, una wiki per ogni cliente nel
  proprio container, domini personalizzati, collaboratori, quote e scadenze,
  accesso con l'account della piattaforma, un'API dedicata e backup su Google
  Drive. Le nuove wiki ottengono subito un certificato HTTPS, anche dietro
  Cloudflare, e le wiki ferme vengono riavviate automaticamente.

## Per iniziare

### Dal codice sorgente

Ti servono Python 3.11 o successivo e Git.

```sh
git clone https://github.com/BananaSuite/BananaWiki.git
cd BananaWiki
python3 -m venv .venv && . .venv/bin/activate
python -m pip install -e .
bananawiki serve
```

Poi apri <http://127.0.0.1:5001>. Per creare il primo account, esegui
`bananawiki setup-token` in un secondo terminale, nella stessa cartella e
nello stesso ambiente virtuale, e inserisci il codice che stampa.

### Docker Compose

Avvia una wiki dietro Caddy con HTTPS automatico. Per costruire l'immagine
serve Docker BuildKit (`docker buildx`).

```sh
git clone https://github.com/BananaSuite/BananaWiki.git
cd BananaWiki
WIKI_DOMAIN=wiki.example.org ACME_EMAIL=tu@example.org docker compose up -d
docker compose exec wiki python -m bananawiki.cli setup-token
```

### Server Linux

L'installazione configura servizi systemd, HTTPS, aggiornamenti e backup.
Installa dall'ultimo tag di rilascio:

```sh
git clone https://github.com/BananaSuite/BananaWiki.git
cd BananaWiki
git checkout "$(git tag -l 'v*' | sort -V | tail -1)"
sudo ./banana install --mode wiki --domain wiki.example.org
sudo bananawiki proxy --install --email tu@example.org
sudo bananawiki setup-token
```

Per installare invece la piattaforma di hosting, usa `--mode hosting`. Serve
Docker con BuildKit. Va bene qualsiasi filesystem, ma su XFS montato con
`prjquota` ogni wiki ha anche un limite di spazio rigido. Gli aggiornamenti
automatici sono disattivati finché non li attivi con
`sudo bananawiki updates enable --require-signatures ALLOWED_SIGNERS`.

### Desktop

[BananaWiki Desktop](docs/desktop.md) fa funzionare una wiki su un computer di
classe o d'ufficio, senza server.

Se aggiorni dalla 1.6 o dalla 1.4, leggi prima [UPGRADING.md](UPGRADING.md)
(in inglese).

## Sviluppo

```sh
python -m pip install -e ".[dev]"
python -m pytest tests -q -n 2
python -m ruff check .
```

Esegui i test con `python -m pytest` invece di `pytest`. I test si importano
a vicenda come pacchetto, quindi la cartella principale del repository deve
essere nel percorso. La CI esegue i test con Python dalla 3.11 alla 3.14.

## Documentazione

La [documentazione](docs/README.md) è in inglese, mentre la
[guida utente](docs/user-guide/it/README.md) è disponibile anche in italiano.

Se vuoi contribuire, parti da [CONTRIBUTING.md](CONTRIBUTING.md) e
[ARCHITECTURE.md](ARCHITECTURE.md) e segui il
[codice di condotta](CODE_OF_CONDUCT.md). Per segnalare un problema di
sicurezza, segui [SECURITY.md](SECURITY.md) invece di aprire una issue
pubblica.

## Licenza

BananaWiki è distribuita con licenza [GNU Affero General Public License
v3.0 only](LICENSE) (`AGPL-3.0-only`). Puoi usarla per scopi personali e
commerciali.

Se usi una versione modificata di BananaWiki a cui altre persone accedono in
rete, la AGPL ti chiede di rendere disponibile a loro il tuo codice sorgente.
Imposta `BW_SOURCE_URL` sull'indirizzo del tuo codice sorgente e la wiki lo
collegherà all'indirizzo `/source`. Se ridistribuisci BananaWiki, conserva le
note di terze parti elencate in [NOTICE](NOTICE).

BananaWiki è stata creata da Luca Zani
([OverloadedTech](https://github.com/OverloadedTech)) nel febbraio 2026.

Copyright © 2026 Luca Zani e collaboratori. Ogni collaboratore conserva il
copyright dei propri contributi.
