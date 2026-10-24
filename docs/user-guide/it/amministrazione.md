# Amministrare la wiki

Un breve giro per i nuovi amministratori. Il riferimento completo è nella
[documentazione](../../README.md) (in inglese), in particolare
[funzionalità](../../features.md), [permessi](../../permissions.md) e
[sicurezza](../../security.md).

## Fiducia

Installare plugin di terze parti e importare un intero sito danno ciascuno il
controllo completo della wiki, per questo solo proprietari e superutenti possono
farlo. Gli altri amministratori gestiscono comunque tutti gli altri account e
tutti i contenuti: assegna il ruolo solo a persone a cui affideresti tutto
questo. Proprietari e superutenti sono protetti dagli altri amministratori: solo
proprietari e superutenti possono modificare un amministratore.

## L'area di amministrazione

Apri **Amministrazione** dal menu dell'account.

* **Pannello**: account in attesa di approvazione, account sospesi, traffico.
* **Utenti**: crea account, approva o rifiuta iscrizioni, cambia i ruoli,
  assegna ruoli personalizzati, imposta permessi individuali e accesso alle
  categorie, sospendi, reimposta password, chiudi sessioni, impersona (per
  vedere la wiki come un'altra persona; viene registrato). La pagina
  **Cronologia** di un account elenca i suoi nomi utente precedenti, che
  nessun altro può prendere: da lì puoi liberarne uno quando serve a un altro
  account.
* **Ruoli personalizzati**: insiemi di permessi e limiti di categoria per
  gruppi di persone.
* **Codici d'invito**: codici per l'iscrizione, con numero di usi, scadenza
  e ruolo.
* **Sessioni**: tutte le sessioni attive; disconnetti tutti; disconnessione
  automatica giornaliera.
* **Impostazioni del sito**: nome, fuso orario, modalità pubblica, iscrizione
  e approvazione, modalità manutenzione, sessione unica, protezione dai bot,
  caricamenti, bozze, esportazioni.
* **Modalità BM**: la modalità semplice. Attivala per tenere la wiki semplice,
  poi scegli quali funzioni restano e cosa diventa più semplice (una barra
  dell'editor più corta, meno azioni sulle pagine, pagine senza cronologia né
  etichette di difficoltà, una barra laterale con solo pagine, Kanban e
  canvas, impostazioni più brevi, un menu di amministrazione breve, Kanban e
  canvas più semplici). Se è disattivata per la wiki, i
  singoli account possono comunque usarla: decidi se le persone possono
  cambiarla da sole e se i nuovi account partono in modalità BM, metti dentro
  o fuori in una volta gli account dei ruoli che spunti (utenti, editor,
  amministratori), oppure cambiala per un account (e se può cambiarla da sé)
  dalla sua pagina in **Utenti**.
* **Aspetto** e **Lingue**: colori, favicon, lingue dell'interfaccia e gli
  easter egg della mascotte.
* **Plugin**: attiva e disattiva le funzioni (anche il costruttore di pagine) e
  ordina il menu App. Disattivare una funzione la nasconde ma ne conserva dati
  e impostazioni.
* **Documentazione**: aggiunge la guida integrata (completa o semplificata,
  in inglese, italiano o tedesco) come categoria di pagine.
* **Migrazione del sito**: esporta l'intera wiki oppure importane una (questo
  sostituisce tutto, account compresi).
* **Eliminazione in blocco**, **Markdown in blocco**, **Registro di
  controllo**, **Server** e le pagine delle singole funzioni (annunci,
  distintivi, chat, lettura ad alta voce…).

## Buone abitudini

* Controlla regolarmente i nuovi account e l'elenco degli amministratori.
* Guarda il registro di controllo alla ricerca di azioni insolite.
* Verifica che i backup funzionino e prova ogni tanto un ripristino.
* Disattiva le funzioni che la tua wiki non usa.
* Segnala chiaramente le pagine superate, oppure nascondile.
