# Cos'è BananaWiki?

> Una panoramica semplice di come funziona BananaWiki, senza tecnicismi.

BananaWiki è come un quaderno condiviso salvato sul server del tuo team.
Chiunque inviti tu lo può leggere, le persone di cui ti fidi lo possono
modificare, e nessun altro lo può vedere.

Questa guida è scritta per chi vuole usare una wiki, anche senza essere
uno sviluppatore. Non serve sapere cosa sia Flask, SQLite o Markdown per
capirla. Se appare un termine tecnico inevitabile, lo spieghiamo in
parole semplici la prima volta.

## Cos'è una "wiki"?

Una *wiki* è un sito fatto di **pagine** che le persone che la usano
possono modificare loro stesse. Wikipedia è l'esempio più famoso, ma la
maggior parte delle wiki sono piccole e private, per un'azienda, una
scuola, un gruppo di amici o un singolo progetto.

Ogni pagina ha:

- un **titolo** (per esempio *Onboarding* o *Calendario ferie*);
- un **corpo** di testo che chiunque abbia i permessi può modificare;
- una **cronologia**, così puoi sempre vedere chi ha cambiato cosa e
  tornare a una versione precedente.

Il corpo si scrive in **Markdown**, in pratica testo normale con
qualche simbolo per **grassetto**, *corsivo* o elenchi puntati.
L'editor ha pulsanti per tutto, quindi non devi imparare i simboli a
memoria.

## Cosa rende BananaWiki diverso?

BananaWiki è completamente self-hosted: la wiki, la ricerca, la chat e
gli upload girano tutti su un server che controlli tu.

Include:

- categorie che funzionano come cartelle per raggruppare pagine
  correlate;
- una ricerca che filtra pagine e categorie mentre digiti;
- una chat per messaggi diretti e conversazioni di gruppo;
- bacheche Kanban per tracciare il lavoro, con colonne, ticket e
  allegati;
- la prenotazione delle pagine, così due persone non si pestano i piedi
  modificando la stessa pagina contemporaneamente;
- ruoli per decidere chi può leggere, chi può scrivere e chi
  amministra il sito.

Non sei obbligato a usare niente di tutto questo. Ogni funzionalità si
può attivare o disattivare dal pannello di amministrazione.

## Com'è una giornata tipo con la wiki?

1. Apri la wiki nel browser e fai login.
2. La home mostra le **categorie** nella barra di sinistra e gli
   aggiornamenti più recenti al centro.
3. Clicchi una categoria per aprirla e una pagina per leggerla.
4. Se vedi un refuso o vuoi aggiungere qualcosa, clicchi **Modifica**
   in alto.
5. L'editor si divide in due: tu scrivi a sinistra, l'anteprima dal
   vivo appare a destra.
6. Salvi. La pagina aggiunge una voce alla **Cronologia**, così tu (o
   chiunque altro) puoi sempre vedere cosa è stato cambiato.

## Chi può fare cosa?

BananaWiki usa quattro ruoli, dal meno al più potente:

| Ruolo | Cosa può fare |
| --- | --- |
| **User** | Legge le pagine, chatta, commenta, ma non può modificare le pagine. |
| **Editor** | Tutto ciò che fa un User, più crea, modifica ed elimina pagine. |
| **Admin** | Tutto ciò che fa un Editor, più gestisce utenti, impostazioni e plugin. |
| **Protected admin** | Come l'Admin, ma gli altri admin non possono né rimuoverlo né degradarlo. Utile come account "di emergenza". |

Gli admin possono anche personalizzare i ruoli per singolo utente:
per esempio puoi dare a uno User il diritto di caricare immagini senza
renderlo Editor a tutti gli effetti.

## La guida utente integrata

Ogni BananaWiki include una guida utente integrata che gli admin
possono pubblicare nella wiki con un click da
**Admin → Site Settings → Wiki Documentation → Spawn documentation**.
Crea una categoria chiamata *BananaWiki* con pagine come *Benvenuto*,
*Pagine e modifica*, *Ruoli e permessi*, *Chat*, *Kanban* e così via.

Puoi:

- leggerla dentro la wiki, come qualsiasi altra pagina;
- scaricarla come ZIP dallo stesso pannello, modificare i file
  Markdown in locale e ricaricarli con **Bulk Markdown Import** per
  avere una versione personalizzata adatta al tuo team;
- sfogliare lo stesso testo su disco nella cartella
  [`docs/user-guide/`](user-guide/) del codice sorgente: la copia
  inglese è in [`docs/user-guide/en/`](user-guide/en/) e quella
  italiana in [`docs/user-guide/it/`](user-guide/it/).

La copia su disco non viene allineata automaticamente. Dopo aver
modificato la guida in `db/_wiki_docs.py`, esegui a mano
`python scripts/sync_user_guide_docs.py` per riallineare
`docs/user-guide/`. Vedi
[`docs/user-guide/it/README.md`](user-guide/it/README.md).

## Da dove partire

- Se usi BananaWiki per la prima volta, leggi
  [`docs/user-guide/it/bananawiki-welcome.md`](user-guide/it/bananawiki-welcome.md).
- Per configurare una nuova wiki, segui
  [`docs/getting-started.md`](getting-started.md) (in inglese, ma con
  comandi universali).
- Se gestisci la wiki per un team, apri
  [`docs/permissions.md`](permissions.md) e
  [`docs/operations.md`](operations.md).
- Per le API, scorri
  [`docs/api.md`](api.md).

Se qualcosa qui non è chiaro, è un bug della documentazione. Apri
un'issue o una pull request, così lo sistemiamo per la prossima persona.
