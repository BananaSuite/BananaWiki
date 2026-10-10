# Guida amministratore

Gli admin gestiscono impostazioni del sito, utenti, documentazione integrata, operazioni sui contenuti e plugin.

## Impostazioni di base

In **Admin -> Impostazioni sito** puoi cambiare nome del sito, colori, tema predefinito, favicon, fuso orario, lingua dell'interfaccia, iscrizioni, manutenzione e molti valori predefiniti. La maggior parte delle modifiche è immediata.

## Utenti e accessi

Usa **Admin -> Utenti** per creare account, reimpostare password, cambiare ruoli, sospendere utenti, assegnare ruoli personalizzati e regolare permessi. I codici invito limitano le iscrizioni. I limiti di sessione evitano che lo stesso account venga usato in più posti contemporaneamente.

## Ciclo di vita della documentazione

La documentazione integrata può essere creata come categoria BananaWiki. Puoi scegliere guida completa o semplificata, in inglese, italiano o tedesco. Il download ZIP produce file Markdown modificabili e reimportabili con Importazione Markdown in blocco.

## Operazioni sui contenuti

Gli admin possono gestire categorie, ripristinare o rimuovere eliminazioni rallentate, importare Markdown in blocco, esportare pagine, generare PDF e usare gli strumenti di migrazione per backup e ripristino completi.

Un'esportazione completa del sito contiene l'hash della password di ogni account, e un'importazione completa sostituisce tutti gli account. Entrambe chiedono di nuovo la tua password e vengono registrate nel log.

## Proprietari e fiducia

Il ruolo di proprietario impedisce agli altri admin di retrocedere o eliminare l'account dalle normali pagine di amministrazione. Non ferma però un admin che importa un backup completo del sito o installa un plugin: in entrambi i casi ottiene il controllo completo della wiki, compresi tutti gli account. Assegna il ruolo di admin solo a persone di cui ti fidi del tutto.

## Plugin

I plugin controllano funzioni opzionali. Attiva solo ciò che la tua wiki usa davvero e rivedi la lista quando il team cresce. Disattivare un plugin nasconde o ferma la funzione, ma non elimina necessariamente i dati già salvati.

## Routine consigliata

- controlla nuovi utenti e account admin
- leggi gli audit recenti per azioni insolite
- mantieni backup aggiornati e prova il ripristino
- archivia o segnala i contenuti obsoleti
- aggiorna questa documentazione quando il workflow locale cambia
