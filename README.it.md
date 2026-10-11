<img src="bananawiki/wiki/static/favicons/apple-touch-icon.png" alt="Logo di BananaWiki" width="90" height="90">

# BananaWiki

BananaWiki è una wiki self-hosted per gruppi di lavoro, scuole e comunità:
pagine in Markdown con cronologia, permessi dettagliati e strumenti di
collaborazione facoltativi (chat, bacheche kanban, canvas, quiz, lettura ad
alta voce). Funziona come wiki singola, come app desktop su un solo computer
oppure come piattaforma hosting che crea una wiki separata per ogni cliente.

[English](README.md) · [Documentazione (in inglese)](docs/README.md) ·
[Guida utente](docs/user-guide/it/README.md) · [Aggiornare](UPGRADING.md)

## Funzionalità

* **Pagine**: editor Markdown con barra degli strumenti, anteprima, caricamento
  di immagini, tabelle, video e `@menzioni`; categorie
  riordinabili con il trascinamento e navigazione sequenziale; ricerca nel
  testo; rilevamento dei conflitti di modifica; cronologia completa con
  differenze, ripristino e attribuzione; bozze salvate automaticamente;
  allegati; esportazione in PDF e Markdown; importazione ed esportazione in
  blocco.
* **Controllo degli accessi**: ruoli (utente, editor, amministratore,
  proprietario), ruoli personalizzati, permessi per singolo utente, accesso in
  lettura e scrittura per categoria, modalità pubblica, codici d'invito,
  iscrizione aperta con scadenza, approvazione dei nuovi account, sospensioni,
  gestione delle sessioni, registro di controllo.
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
  personalizzate a qualsiasi indirizzo libero, plugin di terze parti (anche
  quelli scritti per la 1.4).
* **Amministrazione**: temi grafici, lingue dell'interfaccia (inglese,
  italiano e tedesco incluse, altre caricabili), guida utente integrata,
  esportazione e importazione dell'intero sito, eliminazione in blocco.
* **Operatività**: installazione con un comando e configurazione HTTPS,
  aggiornamenti con backup e ripristino automatico in caso di errore, backup
  cifrati in un repository Git privato, Docker Compose, app desktop per
  Windows, macOS e Linux.
* **Piattaforma hosting**: registrazione, una wiki per cliente in container
  isolati, domini personalizzati, collaboratori, quote e scadenze, accesso con
  l'account della piattaforma, API propria, backup su Google Drive.

## Per iniziare

**Dal codice sorgente** (Python 3.11 o successivo):

```sh
git clone https://github.com/BananaSuite/BananaWiki.git
cd BananaWiki
python3 -m venv .venv && . .venv/bin/activate
python -m pip install -e .
bananawiki serve
```

Apri <http://127.0.0.1:5001>, esegui `bananawiki setup-token` in un secondo
terminale e inserisci il codice per creare il primo account.

**Docker Compose** (wiki dietro Caddy con HTTPS automatico; per costruire
l'immagine Docker deve avere il componente BuildKit, `docker buildx`):

```sh
WIKI_DOMAIN=wiki.example.org ACME_EMAIL=tu@example.org docker compose up -d
docker compose exec wiki python -m bananawiki.cli setup-token
```

**Server Linux gestito** (servizi systemd, HTTPS, aggiornamenti, backup):

```sh
sudo ./banana install --mode wiki --domain wiki.example.org
sudo bananawiki proxy --install --email tu@example.org
sudo bananawiki setup-token
```

Con `--mode hosting` si installa la piattaforma hosting (serve Docker con
BuildKit; va bene qualsiasi filesystem, e su XFS montato con `prjquota` ogni
wiki ha anche un limite di spazio rigido). Le nuove
installazioni seguono i tag di rilascio (`vX.Y.Z`); per un server di
produzione, prima di installare passa anche all'ultimo tag di rilascio
(`git checkout v1.7.0`). Gli aggiornamenti automatici restano disattivati
finché non esegui
`sudo bananawiki updates enable --require-signatures ALLOWED_SIGNERS`.

**Desktop**: [BananaWiki Desktop](docs/desktop.md) fa funzionare una wiki su
un computer di classe o d'ufficio, senza server.

Per aggiornare dalla 1.6, o un'installazione 1.4, leggi [UPGRADING.md](UPGRADING.md) (in inglese).

## Come è nata

Luca Zani ([OverloadedTech](https://github.com/OverloadedTech)) ha avviato
BananaWiki il 20 febbraio 2026 come wiki per un solo gruppo. Nei mesi
successivi è diventata una piattaforma che crea e gestisce una wiki per ogni
tenant, con le approvazioni, le quote e i domini personalizzati che questo
comporta. La pagina [Chi siamo](https://bananawiki.com/about/index.it.html)
racconta la storia per intero.

Fino a settembre 2026 era un progetto privato. Con la prima pubblicazione è
diventata software libero: lo stesso codice fa girare il servizio hosting e
chiunque può installarlo sul proprio server. La versione 1.6 è una
riscrittura di quella pubblicazione che ne conserva dati, indirizzi e
configurazione, così le installazioni esistenti si aggiornano sul posto.

Il codice è stato sviluppato in privato per circa sette mesi e quella
cronologia conta poco più di tremila commit. Non viene pubblicata: contiene
credenziali di deployment e note sull'infrastruttura, e non c'è modo di
rimuovere con certezza ogni segreto da così tanti commit. Il repository
pubblico parte quindi da un'esportazione pulita; il file [NOTICE](NOTICE)
conserva la data di inizio e l'hash del primo commit.

## Licenza e titolarità

BananaWiki è distribuita con la GNU Affero General Public License versione 3
(`AGPL-3.0-only`); vedi [LICENSE](LICENSE). L'uso personale e commerciale è
consentito, senza chiavi di registrazione né dichiarazioni di uso
commerciale.

Se modifichi BananaWiki e la rendi usabile in rete, la sezione 13 della AGPL
ti chiede di offrire agli utenti il codice sorgente corrispondente. Imposta
`BW_SOURCE_URL` sul sorgente della versione che usi: la wiki lo collega
all'indirizzo `/source`. Conserva le note di terze parti.

Copyright © 2026 Luca Zani e chi contribuisce. Ogni persona che contribuisce
conserva il copyright dei propri contributi. Sorgente:
<https://github.com/BananaSuite/BananaWiki>.

## Manutentori

Avviata da Luca Zani; mantenuta da Luca Zani e da chi contribuisce. Per
partecipare, parti da [CONTRIBUTING.md](CONTRIBUTING.md) (in inglese).
