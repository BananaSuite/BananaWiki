<img src="app/static/favicons/banana_yellow.png" alt="Logo di BananaWiki" width="64">

# BananaWiki

BananaWiki è una wiki self-hosted con cronologia delle pagine, permessi, chat, bacheche kanban, diagrammi canvas e plugin.

Puoi installare una singola wiki oppure una piattaforma hosting che gestisce istanze separate. La piattaforma offre approvazioni, sospensioni, cronologia delle decisioni amministrative, esportazioni e domini personalizzati autorizzati dall'amministratore.

Sui server Linux usa `sudo ./banana install --mode wiki` oppure `--mode hosting`. I comandi `bananawiki update`, `backup`, `restore` e `uninstall` ricordano la modalità scelta. Gli aggiornamenti automatici sono disattivati inizialmente: `bananawiki updates enable` li abilita e `updates disable` li disabilita. Puoi scegliere repository, branch e fallback, anche su repository privati con token o chiave SSH.

I [backup cifrati in un repository privato GitHub o Forgejo](docs/backups.md) sono facoltativi e supportano sia la wiki singola sia la piattaforma hosting, incluso il sito statico gestito. Conserva la chiave di recupero anche fuori dal server e prova il ripristino prima di attivare la pianificazione. La piattaforma hosting può anche salvare i backup su Google Drive.

## Come è nata

Luca Zani ([OverloadedTech](https://github.com/OverloadedTech)) ha avviato BananaWiki in autonomia il 20 febbraio 2026, quando a Canalescuola serviva una wiki per il progetto dell'Officina Tecnologica. Lo sviluppo ha accelerato a giugno 2026 durante un percorso di FSL (formazione scuola-lavoro), e da allora il progetto è mantenuto da Luca e dall'Officina. In quei mesi una wiki per un solo gruppo è diventata una piattaforma che crea e gestisce una wiki per ogni tenant, con le approvazioni, le quote e i domini personalizzati che questo comporta.

Fino a settembre 2026 era un progetto interno. Con questa pubblicazione la versione interna diventa software libero: lo stesso codice fa girare il servizio hosting, che a volte è aperto al pubblico e a volte no, e chiunque può installarlo sul proprio server.

Accanto sono cresciuti due progetti più piccoli. [BananaChat](https://github.com/BananaSuite/BananaChat) è nato come BananaAI, dalla voglia di far girare in proprio modelli di intelligenza artificiale, e oggi è uno strumento separato per provare un modello linguistico in locale. [BananaVibe](https://github.com/BananaSuite/BananaVibe) serve a provare in fretta nuove funzioni e a seguire la manutenzione ordinaria, e si ferma sempre a una pull request in bozza che una persona rivede.

## Documentazione

[L'indice completo è in `docs/`](docs/README.md). Le pagine più usate:

- [Installazione e configurazione del servizio](docs/deployment.md)
- [Guida utente in italiano](docs/user-guide/it/README.md)
- [Domini personalizzati e DNS](docs/custom-domains.md)
- [Migrazione da una versione precedente](MIGRATION.md)
- [Guida del progetto in inglese](README.md)
- [Come contribuire](CONTRIBUTING.md), [codice di condotta](CODE_OF_CONDUCT.md) e [segnalazioni di sicurezza](SECURITY.md)

La cartella `site/` contiene una pagina statica modificabile per la home di un servizio hosting. Il sito bananawiki.com è gestito in un repository privato separato; le guide del portale hosting sono in questo repository, in `hosting/content/`.

Il codice è distribuito con licenza GNU AGPL versione 3 (`AGPL-3.0-only`), che consente anche l'uso commerciale. Non occorrono chiavi di registrazione. Chi rende disponibile in rete una versione modificata deve offrire agli utenti il relativo codice sorgente secondo la sezione 13 della licenza. Configura `BW_SOURCE_URL` con il collegamento al codice della versione distribuita.

Il repository pubblico parte da un solo commit. Lo sviluppo è avvenuto per mesi in un repository privato, la cui cronologia contiene credenziali di deploy e dettagli sull'infrastruttura non destinati a lettori esterni. Invece di riscrivere migliaia di commit sperando di non dimenticare nulla, il codice è stato pubblicato come esportazione pulita del sorgente attuale. Le date originali restano in [NOTICE](NOTICE).

Copyright © 2026 Luca Zani e tutti i contributori. Ogni contributore conserva i diritti sui propri contributi. Vedi anche [LICENSE](LICENSE).
