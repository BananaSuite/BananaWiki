# Plugin

La maggior parte delle funzioni di BananaWiki sono plugin che un amministratore attiva o disattiva.

## Gestire i plugin

Apri **Amministrazione → Plugin** per attivare, disattivare, ispezionare, importare o rimuovere pacchetti plugin. I plugin integrati sono distribuiti con BananaWiki. I pacchetti esterni `.bwplugin` vengono eseguiti con tutti i diritti della wiki: sono disattivati finché chi gestisce il server non li consente (`BW_ALLOW_EXTERNAL_PLUGINS=1`) e solo il proprietario o un superutente può importarli o attivarli. Gli altri amministratori possono comunque disattivare e rimuovere i plugin.

## Disattivare un plugin

Disattivare un plugin ne nasconde pagine e menu. I suoi dati e le sue impostazioni restano e tornano quando lo riattivi.

## Sviluppare plugin

Un plugin dichiara una funzione come quelle integrate; vedi `docs/plugins/README.md` nel codice sorgente. I plugin scritti per la 1.4 con `bananawiki_sdk` si caricano ancora.
