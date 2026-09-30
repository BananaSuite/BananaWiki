# Plugin ed Estensioni

I plugin permettono a BananaWiki di crescere senza obbligare ogni wiki a usare ogni funzione. I plugin integrati coprono collaborazione, governance dei contenuti, automazione, integrazioni e strumenti sperimentali.

## Gestire i plugin

Apri **Admin -> Plugin** per attivare, disattivare, ispezionare, importare o rimuovere pacchetti plugin. I plugin integrati sono distribuiti con BananaWiki. I pacchetti esterni `.bwplugin` possono essere importati quando il deployment lo consente.

## Cosa possono aggiungere

Un plugin può aggiungere route, template, file statici, permessi, hook, tabelle database, slot template, API o lavori in background. Alcuni plugin sono piccole funzioni UI; altri sono sottosistemi completi.

## Attiva con intenzione

Attiva gli strumenti che il team è pronto a usare. Ogni funzione aggiunge superficie, permessi e domande di supporto. Parti con pochi plugin e aggiungine altri quando i workflow sono chiari.

## Dati e disattivazione

Disattivare un plugin di solito nasconde o ferma la funzione, ma non garantisce la cancellazione dei dati. Riattivandolo, i dati potrebbero tornare visibili. Cancella o migra dati con una decisione esplicita quando ritiri una funzione.

## Sviluppare plugin

Gli sviluppatori possono usare il BananaWiki SDK per registrare hook, permessi, slot template e helper database. Mantieni i plugin circoscritti, documentati e reversibili così gli admin capiscono l'impatto prima di abilitarli.
