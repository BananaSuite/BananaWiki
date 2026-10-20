# API REST

Il plugin **API REST** aggiunge endpoint JSON sotto `/api/v1/` per script e integrazioni. Le richieste portano un token Bearer. L'elenco completo degli endpoint è in `/api-docs` sulla tua wiki. Gli endpoint sotto `/api/` usati dall'interfaccia web sono interni e possono cambiare tra una versione e l'altra.

## Attivarla

Un amministratore attiva il plugin in **Amministrazione → Plugin**, poi spunta *Attiva l'API REST* nella sua pagina di impostazioni, dove si impostano anche i limiti di richieste e il numero di token per account.

## Token

Ogni account crea i suoi token in **Impostazioni → Token API**. Un token viene mostrato una sola volta; revoca i token che non usi più.

## Ambiti

Un token raggiunge solo gli endpoint dei suoi ambiti: `pages`, `categories`, `kanban`, `canvas`, `tokens`, e per gli amministratori `users`, `settings` e `admin`. `userbot` è per la chiave di automazione.

## Cosa può fare un token

Un token agisce come la persona a cui appartiene e non può mai fare più di quanto quella persona potrebbe fare nell'editor. Valgono le restrizioni di lettura e scrittura per categoria: una pagina fuori dalle categorie leggibili risulta inesistente, e una modifica fuori dalle categorie scrivibili viene rifiutata con `403`. Una pagina protetta, o prenotata da un altro editor, viene rifiutata con `409`. L'elenco delle pagine contiene solo quelle leggibili. Creare, rinominare e spostare categorie richiede gli stessi permessi di categoria della wiki.

Eliminare una pagina tramite API segue le stesse regole del pulsante Elimina. Con l'eliminazione ritardata attiva la pagina entra nel periodo di attesa e la richiesta risponde `202` con `"pending_deletion": true` invece di rimuoverla.
