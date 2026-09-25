# Guida utente di BananaWiki

Questa cartella contiene la versione su disco della guida utente di BananaWiki. È lo stesso testo che viene creato all'interno della wiki dalla funzione **Spawn documentation** (Admin → Impostazioni del sito → Wiki Documentation), e viene generato automaticamente dal modulo Python `db/_wiki_docs.py` tramite `scripts/sync_user_guide_docs.py`.

Per modificare la guida:

1. Modifica direttamente i file `.md` di questa cartella, oppure modifica le stringhe dentro `db/_wiki_docs.py`.
2. Esegui `python scripts/sync_user_guide_docs.py` per riallineare i due lati (la sorgente Python è il riferimento ufficiale).
3. Per pubblicare le modifiche dentro una wiki reale, usa **Admin → Site Settings → Wiki Documentation → Download ZIP**, modifica i file in locale e re-importali con **Bulk Markdown Import**, oppure ri-spawna la documentazione standard.

## Pagine

- [Benvenuto in BananaWiki](bananawiki-welcome.md)
- [Pagine e Modifica](bananawiki-pages-editing.md)
- [Categorie e Navigazione](bananawiki-categories-navigation.md)
- [Ruoli e Permessi](bananawiki-roles-permissions.md)
- [Guida amministratore](bananawiki-admin-guide.md)
- [Chat e Messaggistica](bananawiki-chat-messaging.md)
- [Bacheche Kanban](bananawiki-kanban-boards.md)
- [Layout Canvas](bananawiki-canvas.md)
- [Badge e Traguardi](bananawiki-badges.md)
- [Plugin ed Estensioni](bananawiki-plugins.md)
- [Riferimento API per sviluppatori](bananawiki-api-reference.md)
- [Sicurezza e Backup](bananawiki-security.md)
