# Lavorare insieme

Questi strumenti compaiono sotto **App** quando la tua wiki li ha attivati.

## Messaggi e gruppi

* **Messaggi** (`/chats`): avvia una conversazione con una persona inserendo
  il suo nome utente.
* **Gruppi** (`/groups`): crea un gruppo o entra in uno con il suo codice
  d'invito. Quasi tutte le wiki hanno anche una stanza globale per tutti. I
  proprietari e i moderatori possono aggiungere e rimuovere membri, mettere
  in pausa o bandire qualcuno e cambiare il codice d'invito; il proprietario
  può cedere il gruppo.
* Puoi allegare file (entro i limiti di dimensione e giornalieri), esportare
  una conversazione ed eliminare i tuoi messaggi. **Eliminare un messaggio lo
  cancella per tutti**; resta un piccolo segno "eliminato".
* Gli amministratori possono leggere le conversazioni per la moderazione, e
  la wiki può cancellare automaticamente messaggi e file vecchi dopo un certo
  tempo.

Usa la chat per conversare e le pagine per il sapere: quando una discussione
decide qualcosa, scrivilo in una pagina.

## Bacheche kanban

**Kanban** (`/kanban`) contiene bacheche fatte di colonne e schede.

* Aggiungi colonne (per esempio *Da fare*, *In corso*, *Fatto*) e schede;
  trascinale per riordinarle o spostarle. Chi guarda la bacheca vede le
  modifiche in pochi secondi.
* Una scheda ha una descrizione (Markdown), etichette, una priorità, una
  scadenza, un colore, assegnatari, commenti, allegati e una sua cronologia.
* Nel titolo di una scheda puoi usare abbreviazioni: `Correggi login @alice
  +backend !high color:red due:tomorrow` assegna Alice, aggiunge l'etichetta,
  imposta priorità, colore e scadenza (`today`, `tomorrow`, `nextweek` o
  `AAAA-MM-GG`).
* Una scheda può contenere una **checklist** di passi più piccoli; la scheda
  mostra quanti sono completati (☑ 2/5).
* La **barra dei filtri** sopra la bacheca la restringe per testo, persona (o
  *Io*), etichetta, priorità e scadenza (scadute, entro 2 giorni, prossimi 7
  giorni, senza scadenza). Premi `/` per andare al campo di ricerca; il
  filtro resta nell'indirizzo, quindi puoi salvarlo nei preferiti o
  condividerlo. Le schede in scadenza sono evidenziate in giallo, quelle
  scadute in rosso.
* **Limite** nell'intestazione di una colonna imposta un limite di lavoro in
  corso: la colonna viene evidenziata quando contiene più schede di così.
* **Righe** sopra la bacheca la divide in corsie per assegnatario, priorità o
  etichetta. Trascina una scheda in un'altra riga per riassegnarla o
  cambiarne la priorità (da tastiera: spostala oltre la prima o l'ultima
  scheda della riga).
* **Archivia** una scheda dalla sua finestra, più schede insieme con
  **Selezione**, oppure tutte le schede di una colonna con **Archivia**
  nell'intestazione della colonna (comodo per *Fatto*). Le schede archiviate
  escono dalla bacheca ma conservano tutto; il pulsante **Archiviate** le
  elenca per aprirle, ripristinarle o eliminarle definitivamente.
* Chi ha creato la bacheca può **archiviare l'intera bacheca**: sparisce
  dall'elenco (usa *Mostra le bacheche archiviate* per ritrovarla) e resta in
  sola lettura finché non viene ripristinata.
* **Le mie schede** (`/kanban/mine`) elenca ciò che è assegnato a te su tutte
  le bacheche che puoi aprire, raggruppato per scadenza; il modulo in alto la
  filtra.
* Le voci della checklist si possono trascinare dalla maniglia (⠿) o spostare
  con le frecce su di essa.
* Tastiera: seleziona la maniglia (⠿) di una scheda e usa le frecce per
  spostarla tra posizioni e colonne; ogni spostamento viene annunciato ai
  lettori di schermo.
* **Cronologia** mostra gli stati precedenti della bacheca, con i limiti delle
  colonne, le checklist e le schede archiviate, e cosa è cambiato in
  ciascuno; ripristinarne uno non elimina mai schede, commenti o file.
* Chi ha creato la bacheca può condividerla con persone o ruoli (lettura o
  scrittura), renderla pubblica, esportarla o eliminarla.
* Inserisci una bacheca in una pagina con `[[kanban board="<numero>"]]`.

## Canvas

I **Canvas** (`/canvas`) sono lavagne libere: note, forme, immagini, video,
codice e collegamenti a pagine della wiki, uniti da frecce. Più persone
possono modificarli contemporaneamente. I canvas hanno una cronologia con
ripristino, si possono condividere con persone o ruoli (lettura o modifica),
esportare e importare e mostrare in una pagina con `[[canvas slug="<nome>"]]`.

* **Parti da un modello**: quando crei un canvas puoi scegliere un diagramma
  di flusso, una mappa mentale, una bacheca per la retrospettiva o un'analisi
  SWOT invece di un canvas vuoto.
* **Disponi**: seleziona più elementi (Maiusc+clic o Maiusc+trascina) e usa
  **Disponi** per allinearli o distribuirli, raggrupparli (Ctrl+G) perché si
  spostino insieme o bloccarli (L) perché restino fermi. Nessuno può
  spostare, ridimensionare, modificare o eliminare un elemento bloccato (il
  wiki lo rifiuta, anche tramite l'API) finché qualcuno non lo sblocca.
  Mentre trascini compaiono **linee guida** quando bordi o centri si
  allineano con gli elementi vicini, e l'elemento vi si aggancia; altrimenti
  **Aggancia alla griglia** (G) allinea gli elementi. Tieni premuto Alt per
  posizionarli liberamente.
* I **collegamenti** seguono gli elementi quando li sposti; con un doppio clic
  scegli un percorso curvo, dritto o ad angoli retti, le frecce,
  un'etichetta e il lato di ciascun elemento a cui si attaccano (alto,
  destra, basso, sinistra o automatico).
* La **mappa d'insieme** nell'angolo mostra tutto il canvas; fai clic per
  spostarti.
* **Immagine** scarica il canvas come PNG o SVG. Le immagini di altri siti
  sono incluse quando il loro sito lo consente; altrimenti il PNG mostra un
  riquadro segnaposto con etichetta (l'SVG mantiene un link all'immagine).
  **Schema testuale** elenca
  ogni elemento con i suoi collegamenti, leggibile con un lettore di schermo
  e scaricabile in Markdown.

## Quiz

Una pagina può avere un quiz (domande a risposta singola, multipla o
libera). Aprilo dalla pagina, rispondi e invia: il punteggio compare subito.
A seconda del quiz puoi riprovare. **I miei punti quiz** nel menu
dell'account elenca i tuoi risultati. Gli editor con il permesso adatto creano
e gestiscono i quiz e vedono i risultati di tutti.

## Annunci

Fasce in cima alla wiki con le notizie degli amministratori. Puoi chiudere
una fascia; ricompare se viene modificata.

## Distintivi e classifica

I distintivi sul tuo profilo riconoscono contributi e traguardi; alcuni si
ottengono automaticamente (prima modifica, numero di modifiche, tempo di
lettura, giorni di iscrizione), altri li assegnano gli amministratori. Ricevi
una notifica quando ne ottieni uno. La **Classifica** ordina i contributori
in un periodo.

## Pagine federate

Se la tua wiki è collegata ad altre installazioni di BananaWiki, **Pagine
federate** mostra le pagine che ti hanno condiviso (in sola lettura), e gli
editor possono condividere pagine con loro.
