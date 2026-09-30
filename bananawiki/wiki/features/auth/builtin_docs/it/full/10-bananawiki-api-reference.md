# Riferimento API per sviluppatori

BananaWiki espone superfici per automazione, plugin e integrazioni. Alcuni endpoint usano sessione web e token CSRF. Il plugin API Service aggiunge endpoint Bearer-token per client esterni.

## Endpoint basati su sessione

Gli endpoint web comuni includono ricerca, anteprima Markdown, bozze, filtro sidebar, prenotazioni pagina, riordino Kanban, dati Canvas, preferenze di accessibilità e upload. Richiedono un utente autenticato e le mutazioni richiedono protezione CSRF.

## Plugin API Service

Quando è attivo, API Service fornisce endpoint `/api/v1/` autenticati con Bearer token. Gli utenti possono creare token personali dove consentito. Gli admin configurano scope, limiti, scadenze e audit.

## Scope

Gli scope limitano cosa può fare un'integrazione. Preferisci il set minimo utile: pages per contenuti, categories per navigazione, users per amministrazione, settings per configurazione, userbot per automazioni account.

## Cosa può fare un token

Un token agisce come la persona a cui appartiene e non può mai fare più di quanto quella persona potrebbe fare nell'editor. Valgono le restrizioni di lettura e scrittura per categoria: una pagina fuori dalle categorie leggibili risulta inesistente, e una modifica fuori dalle categorie scrivibili viene rifiutata con `403`. Una pagina protetta, o prenotata da un altro editor, viene rifiutata con `409`. L'elenco delle pagine contiene solo quelle leggibili. Creare, rinominare e spostare categorie richiede gli stessi permessi di categoria della wiki.

Eliminare una pagina tramite API segue le stesse regole del pulsante Elimina. Con Deletion Slowdown attivo la pagina entra nel periodo di attesa e la richiesta risponde `202` con `"pending_deletion": true` invece di rimuoverla.

## Plugin SDK

Gli autori di plugin possono usare decoratori di autenticazione, registrare permessi, aggiungere slot template, chiamare helper database sicuri e reagire agli hook. Usa l'SDK come livello stabile invece di importare internals privati.

## Checklist di sicurezza

- mantieni segreti i token e ruotali quando una persona lascia il team
- imposta scadenze per integrazioni temporanee
- registra e rivedi le automazioni che modificano contenuti
- valida i dati in ingresso anche quando una route è solo admin
