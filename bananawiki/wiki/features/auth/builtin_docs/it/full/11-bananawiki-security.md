# Sicurezza e backup

Come la wiki protegge account e contenuti, e come farne il backup.

## Protezione degli account

Le password sono salvate come hash, i tentativi di accesso sono limitati e i moduli sono protetti dalle richieste da altri siti. Gli amministratori possono sospendere account, imporre il cambio password, richiedere codici d'invito, consentire una sola sessione per account e mettere la wiki in manutenzione.

Chiunque può attivare la **verifica in due passaggi** in **Impostazioni → Verifica in due passaggi**: dopo la password, l'accesso chiede anche un codice generato da un'app di autenticazione sul telefono, oppure uno dei dieci codici di recupero monouso mostrati all'attivazione. Proprietari e superutenti possono richiederla a tutti gli amministratori in **Amministrazione → Impostazioni del sito → Sicurezza** e azzerarla in **Amministrazione → Utenti** per chi ha perso sia il telefono sia i codici di recupero.

## Protezione dei contenuti

Il Markdown viene ripulito dall'HTML non sicuro prima di essere mostrato. Gli upload vengono controllati rispetto ai limiti di dimensione e ai tipi di file consentiti e bloccati, e le immagini fisse vengono ricodificate, il che rimuove i dati di posizione. Gli allegati vengono serviti solo a chi può leggere la pagina.

## Annullare le modifiche

Il registro di controllo annota accessi, modifiche ad account e ruoli, eliminazioni e modifiche alle impostazioni. La cronologia ripristina le versioni precedenti, e l'eliminazione ritardata conserva le pagine eliminate per 48 ore.

## Backup

Fai i backup con l'esportazione completa del sito o con i backup del server, tieni copie su un'altra macchina e prova un ripristino prima di averne bisogno.

Un'esportazione completa contiene l'hash di ogni password, quindi conservala come una password. Solo proprietari e superutenti possono esportare o importare l'intero sito; entrambe le operazioni chiedono di nuovo la password e vengono registrate nel log. Importare un backup dà il controllo completo della wiki, account dei proprietari compresi, perché il backup sostituisce tutti gli account. Installare un plugin fa lo stesso, perché il suo codice gira con gli stessi diritti della wiki.
