# Sicurezza e Backup

BananaWiki è progettata per conoscenza privata, quindi protegge account e contenuti, registra gli eventi amministrativi e di sicurezza e permette backup e ripristino completi del sito.

## Protezione account

Le password sono salvate come hash, i tentativi di login sono limitati, i token CSRF proteggono form e mutazioni AJAX, e gli admin possono imporre limiti di sessione. Sospensioni, cambio password obbligatorio, codici invito e modalità manutenzione aggiungono altri controlli.

Chiunque può attivare la **verifica in due passaggi** in **Impostazioni → Verifica in due passaggi**: dopo la password, l'accesso chiede anche un codice generato da un'app di autenticazione sul telefono, oppure uno dei dieci codici di recupero monouso mostrati all'attivazione. Proprietari e superutenti possono richiederla a tutti gli amministratori in **Amministrazione → Impostazioni del sito → Sicurezza** e azzerarla in **Amministrazione → Utenti** per chi ha perso sia il telefono sia i codici di recupero.

## Protezione contenuti

Il Markdown viene sanificato prima della visualizzazione. Gli upload sono validati e i tipi pericolosi vengono bloccati dove opportuno. Gli allegati sono serviti da route autenticate, non come file pubblici. Restrizioni di categoria e permessi tengono le pagine sensibili lontane dagli utenti non autorizzati.

## Sicurezza operativa

Gli audit registrano eventi amministrativi e di sicurezza importanti. Il rallentamento eliminazioni può aggiungere un periodo di grazia prima della rimozione definitiva. Cronologia pagina e backup danno agli admin una via di recupero.

## Backup

Usa esportazioni complete o backup del server. Un backup è utile solo se il ripristino funziona, quindi prova il restore prima di un'emergenza. Conserva copie fuori dal server della wiki live.

Un'esportazione completa contiene l'hash di ogni password, quindi conservala come una password. Esportazione e importazione chiedono di nuovo la password dell'admin e vengono registrate nel log. Importare un backup dà il controllo completo della wiki, account dei proprietari compresi, perché il backup sostituisce tutti gli account. Installare un plugin fa lo stesso, perché il suo codice gira con gli stessi diritti della wiki.

## Produzione

Esegui dietro HTTPS, mantieni l'app aggiornata, limita gli account admin, valuta i plugin prima di abilitarli, monitora lo spazio disco e documenta nella wiki la procedura locale di recupero.
