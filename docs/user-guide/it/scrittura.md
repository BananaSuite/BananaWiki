# Scrivere le pagine

Serve il ruolo di **editor** (o un ruolo personalizzato che lo permetta) e
l'accesso in scrittura alla categoria. Chi può solo leggere spesso può
**proporre una modifica** (vedi [organizzare](organizzazione.md#proporre-una-modifica)).

## Creare una pagina

Fai clic su **Nuova pagina** nella barra laterale (oppure apri `/create`).
Inserisci un titolo, scegli una categoria (o nessuna), scrivi il testo e salva.
L'indirizzo della pagina (`/page/<indirizzo>`) viene ricavato dal titolo; puoi
cambiarlo in seguito.

## Modificare

Apri la pagina e fai clic su **Modifica**. L'editor ha due schede, **Scrivi**
e **Anteprima**, una barra di formattazione (titoli, grassetto, corsivo,
elenchi, citazioni, codice, collegamenti, tabelle, linee orizzontali,
immagini e video) e la modalità a schermo intero. **Ctrl+B** e **Ctrl+I**
funzionano come al solito. Alla fine fai clic su **Salva modifiche** e
aggiungi un breve riassunto di cosa hai cambiato: comparirà nella cronologia.

Se altre persone stanno modificando la stessa pagina, l'editor lo segnala. Se
qualcuno salva la pagina mentre la stai modificando, al salvataggio vieni
avvisato e puoi confrontare le versioni invece di sovrascrivere il suo lavoro.

## Markdown in breve

| Scrivi | Risultato |
|---|---|
| `## Sezione` / `### Sottosezione` | titoli (compaiono nell'indice) |
| `**grassetto**`, `*corsivo*` | enfasi |
| `- voce` o `1. voce` | elenchi (rientra di due spazi per annidare) |
| `[testo](https://example.org)` | collegamento |
| `[testo](/page/altra-pagina)` | collegamento a un'altra pagina |
| `` `codice` `` e blocchi tra righe ```` ``` ```` | codice (evidenziato se indichi il linguaggio) |
| `> citazione` | citazione |
| righe `| a | b |` | tabella |
| `@nome` | collegamento al profilo di quella persona |

Su una riga da sola:

* un indirizzo YouTube o Vimeo diventa un lettore video; `[[video url="…"]]`
  permette di scegliere larghezza, allineamento e proporzioni (il pulsante
  video della barra lo scrive per te);
* `[[kanban board="12"]]` mostra una bacheca kanban, `[[canvas slug="piano"]]`
  un canvas.

L'HTML potenzialmente pericoloso viene rimosso, così le pagine hanno sempre
l'aspetto del resto della wiki.

## Immagini e file

Trascina o incolla le immagini nell'editor per caricarle: vengono salvate
nella wiki e inserite nel punto del cursore. Funzionano anche le immagini di
altri siti, ma caricarle rispetta di più la privacy di chi legge.

Se gli allegati sono attivi, l'editor e la pagina hanno una sezione
**Allegati** per altri file (PDF, documenti, archivi, audio, video). La wiki
può limitare i tipi di file, le dimensioni e quanto puoi caricare al giorno.

## Bozze

Mentre scrivi, l'editor salva una **bozza** privata. Se chiudi la scheda per
sbaglio, riaprendo l'editor ti viene proposto di recuperarla. Le bozze di
altri editor sulla stessa pagina vengono elencate, così sai che qualcuno ha
modifiche non salvate. Salvando la pagina le bozze vengono cancellate e i loro
autori citati. **Le mie bozze** nel menu dell'account elenca le tue; puoi
scartare una bozza o passarla a un'altra persona. Le bozze vecchie possono
scadere.

## Cronologia

**Altro → Cronologia** elenca ogni versione salvata: chi, quando, il
riassunto e l'entità della modifica. Apri una versione per vederla com'era,
nel suo Markdown, oppure le **Modifiche** rispetto alla versione precedente.
Con il permesso adatto puoi **Ripristina questa versione** (il testo attuale
resta nella cronologia), eliminare voci o attribuire una voce a un'altra
persona.

## Il costruttore di pagine

Alcune wiki permettono di costruire una pagina con blocchi visivi (titoli,
testo, immagini, pulsanti, colonne, video) invece di scrivere in Markdown. Se
la tua wiki lo prevede, la pagina offre il costruttore accanto a
**Modifica**. La pagina conserva una copia in Markdown, quindi ricerca e
cronologia continuano a funzionare.
