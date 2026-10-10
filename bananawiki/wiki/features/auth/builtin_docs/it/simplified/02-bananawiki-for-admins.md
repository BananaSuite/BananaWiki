# Per amministratori

Gli admin definiscono come si comporta la wiki. La maggior parte dei controlli si trova in **Admin -> Impostazioni sito**, **Admin -> Utenti** e **Admin -> Plugin**.

## Prime verifiche

- conferma nome del sito, tema, fuso orario e lingua
- decidi se le iscrizioni richiedono codici invito
- crea o invita i primi utenti
- scegli quali plugin servono ora
- fai un backup prima di import o migrazioni importanti

## Utenti e permessi

Usa i ruoli per l'accesso generale e le restrizioni di categoria per decidere dove le persone possono lavorare. Usa ruoli personalizzati quando più persone hanno bisogno dello stesso accesso speciale. Limita gli account admin protetti.

## Documentazione integrata

La sezione **Documentazione Wiki** può creare questa guida dentro la wiki. Scegli guida completa o semplificata, in inglese, italiano o tedesco. Puoi anche scaricare lo ZIP Markdown, modificarlo e importare le pagine personalizzate.

## Backup

Usa l'export di migrazione o i backup di deployment. Le installazioni gestite supportano anche backup cifrati in un repository privato GitHub o Forgejo; consulta `docs/backups.md` nel codice sorgente. L'invio dei backup su Telegram è stato ritirato. Prova il ripristino prima di affidarti a una strategia di backup.
