# Guida amministratore

Gli admin gestiscono impostazioni del sito, utenti, documentazione integrata, operazioni sui contenuti e plugin.

## Impostazioni di base

In **Amministrazione → Impostazioni del sito** puoi cambiare nome del sito, colori, tema predefinito, favicon, fuso orario, lingua dell'interfaccia, iscrizioni, manutenzione e molti valori predefiniti. La maggior parte delle modifiche è immediata.

## Utenti e accessi

Usa **Amministrazione → Utenti** per creare account, reimpostare password, cambiare ruoli, sospendere utenti, assegnare ruoli personalizzati e regolare permessi. I codici invito limitano le iscrizioni. I limiti di sessione evitano che lo stesso account venga usato in più posti contemporaneamente. Proprietari e superutenti possono richiedere la verifica in due passaggi agli amministratori e azzerarla per un account che ha perso l'app di autenticazione e i codici di recupero.

## Ciclo di vita della documentazione

La documentazione integrata può essere creata come categoria BananaWiki da **Amministrazione → Documentazione**. Puoi scegliere guida completa o semplificata, in inglese, italiano o tedesco. Il download ZIP produce file Markdown che puoi modificare sul tuo computer e reimportare con l'importazione Markdown in blocco.

## Operazioni sui contenuti

Gli admin possono gestire categorie, ripristinare o rimuovere eliminazioni rallentate, importare Markdown in blocco, esportare pagine, generare PDF e usare gli strumenti di migrazione per backup e ripristino completi.

Un'esportazione completa del sito contiene l'hash della password di ogni account, e un'importazione completa sostituisce tutti gli account. Per questo solo proprietari e superutenti possono farle; entrambe chiedono di nuovo la tua password e vengono registrate nel log. Le eliminazioni in blocco chiedono di scrivere ELIMINA per conferma.

## Proprietari e fiducia

Il ruolo di proprietario impedisce agli altri admin di retrocedere o eliminare l'account dalle normali pagine di amministrazione. Importare un backup completo del sito o installare un plugin di terze parti dà il controllo completo della wiki, compresi tutti gli account, per questo solo proprietari e superutenti possono farlo. Gli altri admin gestiscono comunque tutti gli altri account e tutti i contenuti: assegna il ruolo di admin solo a persone a cui affideresti tutto questo, e rendi qualcuno proprietario o superutente solo se ti fidi del tutto di quella persona.

## Plugin

I plugin controllano funzioni opzionali. Attiva solo ciò che la tua wiki usa davvero e rivedi la lista quando il team cresce. Disattivare un plugin nasconde o ferma la funzione, ma non elimina necessariamente i dati già salvati.

## Routine consigliata

- controlla nuovi utenti e account admin
- leggi gli audit recenti per azioni insolite
- mantieni backup aggiornati e prova il ripristino
- archivia o segnala i contenuti obsoleti
- aggiorna questa documentazione quando il workflow locale cambia
