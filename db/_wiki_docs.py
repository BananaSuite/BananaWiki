"""Built-in wiki documentation spawning.

Creates a **BananaWiki** category with documentation pages that serve as an
in-app user and admin guide. The content can be spawned in *full* or
*simplified* mode, in English or Italian.

The canonical content lives here as Python data structures. The matching
Markdown files under ``docs/user-guide/<lang>/<slug>.md`` are kept in
sync via ``scripts/sync_user_guide_docs.py`` and verified by
``tests/test_wiki_docs.py::TestDocsFilesInSync`` so the in-app docs and
the on-disk reference never drift apart.
"""

import threading
from datetime import datetime, timezone
from textwrap import dedent

from ._connection import SYSTEM_USER_ID, get_db_context
from ._categories import delete_category, get_category
from ._settings import get_site_settings, update_site_settings

DOCS_CATEGORY_NAME = "BananaWiki"

# Process-level lock so two threads inside the same Gunicorn worker can
# never enter ``spawn_wiki_docs`` concurrently.  Cross-worker / cross-
# process races are additionally serialised by the ``BEGIN IMMEDIATE``
# write lock taken inside the spawn transaction below.
_SPAWN_DOCS_LOCK = threading.Lock()


def _doc(markdown):
    """Normalize an embedded Markdown block for spawned documentation."""
    return dedent(markdown).strip() + "\n"


# The next several hundred lines are page content, not logic: each _*_docs()
# returns (title, slug, content) tuples that spawn_wiki_docs writes as pages.

def _full_docs():
    """Return the full documentation pages (EN) as a list of (title, slug, content) tuples."""
    return [
        (
            "Welcome to BananaWiki",
            "bananawiki-welcome",
            _doc("""
            # Welcome to BananaWiki

            BananaWiki is a private, self-hosted knowledge base for teams that want their notes, guides, plans, and operational memory in one controlled place. Small groups can start writing on day one, and structured organizations can configure permissions, plugins, imports, exports, and automation.

            ## What lives here

            Use BananaWiki for the material that people repeatedly ask for or silently depend on:

            - onboarding notes, policies, checklists, and runbooks
            - project plans, meeting notes, release notes, and decisions
            - internal tutorials, reference pages, and how-to guides
            - team directories, responsibility maps, and process documentation
            - lightweight planning through Kanban boards and Canvas diagrams

            ## How the wiki is organized

            Pages contain Markdown content. Categories arrange pages in the sidebar. Roles and permissions decide who can read, edit, administer, or use specific features. Plugins add optional tools such as chat, badges, attachments, API access, temporary accounts, and text-to-speech.

            ## First steps

            1. Browse the sidebar and open an existing page.
            2. Use search when you know a word from the title or content.
            3. Create a page when knowledge is missing, even if it starts rough.
            4. Move related pages into categories so the next reader can find them.
            5. Ask an admin for the right role or category access if something is hidden.

            ## About this guide

            This documentation is spawned from BananaWiki itself. Admins can recreate it from **Admin -> Site Settings -> Wiki Documentation**, download it as Markdown, customize it, and import the edited version back into the wiki.
            """),
        ),
        (
            "Pages & Editing",
            "bananawiki-pages-editing",
            _doc("""
            # Pages & Editing

            Pages are the basic unit of BananaWiki. A good page has a clear title, a stable URL slug, a useful category, and content that answers a real question for its readers.

            ## Create a page

            1. Click **+ New Page** in the sidebar or the page menu.
            2. Enter a short title. BananaWiki suggests a URL slug automatically.
            3. Choose a category, or leave the page uncategorized while drafting.
            4. Write in Markdown on the left and check the preview on the right.
            5. Save when the page is ready to share.

            You need editor or admin access to create and edit most pages. Admins can also restrict editing to selected categories.

            ## Markdown essentials

            | Write | Result |
            |---|---|
            | `# Heading` | top-level heading |
            | `## Heading` | section heading |
            | `**bold**` | bold text |
            | `*italic*` | italic text |
            | `` `code` `` | inline code |
            | `- item` | bullet list |
            | `1. item` | numbered list |
            | `[label](https://example.com)` | link |
            | `![alt text](image.png)` | image |

            Use headings to make long pages scannable. Put the most important answer near the top, then add detail below it.

            ## Media and rich content

            Images can be uploaded from the editor. Plain YouTube or Vimeo links on their own line are embedded automatically. If the Attachments plugin is enabled, files such as PDFs or ZIP archives can be attached to pages and downloaded by authenticated users.

            ## Drafts, history, and review

            When the Drafts plugin is enabled, your unsaved edits are kept privately while you work. When Page History is enabled, each save records a version so editors can compare, restore, or audit changes later.

            ## Moving and deleting

            Page slugs can be renamed, and internal links are rewritten where possible. Pages can be moved between categories. Deleting a page may be immediate or delayed by the Deletion Slowdown plugin, depending on site settings and whether the page belongs to protected documentation.
            """),
        ),
        (
            "Categories & Navigation",
            "bananawiki-categories-navigation",
            _doc("""
            # Categories & Navigation

            Categories are the folder structure of the wiki. They shape the sidebar, group related pages, and help readers understand where information belongs.

            ## Browse the sidebar

            - Click a category to expand or collapse it.
            - Click a page title to open it.
            - Use the sidebar search field to filter pages and categories quickly.
            - Drag pages and categories when you have permission to reorder them.

            ## Create useful categories

            Prefer category names that match how people look for information: **Operations**, **Product**, **HR**, **Class Notes**, **Release Process**, or **Customer Support**. Avoid deep nesting unless the hierarchy makes navigation easier.

            ## Sequential navigation

            Sequential navigation adds Previous and Next links between pages in a category. It is useful for tutorials, training paths, handbooks, and course chapters. The sequence follows the manual order in the sidebar.

            ## Category permissions

            Admins can limit category visibility and editing. Read restrictions hide categories and pages from users who are not allowed to see them. Write restrictions let an editor work only in selected areas while still reading broader material if permitted.

            ## Deleting a category

            When deleting a category, choose what should happen to its pages: move them elsewhere, uncategorize them, or delete them too. For important content, move first and delete only after checking the result.
            """),
        ),
        (
            "Roles & Permissions",
            "bananawiki-roles-permissions",
            _doc("""
            # Roles & Permissions

            Permissions decide what each person can see and do. BananaWiki starts with simple roles and lets admins add precision when a team needs it.

            ## Built-in roles

            | Role | Typical access |
            |---|---|
            | **User** | read pages, search, use allowed collaboration tools |
            | **Editor** | create and edit pages and categories |
            | **Admin** | manage users, settings, plugins, and site-wide behavior |
            | **Protected admin** | admin access that cannot be changed by regular admins |

            ## Custom roles

            Custom roles combine a base role with specific permissions and category rules. Use them when a built-in role is too broad, for example **Support editor**, **Course assistant**, or **Read-only contractor**.

            ## Per-user overrides

            Admins can override individual permissions for one account. This is helpful for temporary exceptions, but custom roles are easier to maintain when multiple users need the same access.

            ## Category restrictions

            Role permissions answer *what can this person do?* Category restrictions answer *where can they do it?* Combining both lets a user edit one area while only reading another.

            ## Practical guidance

            Start with the smallest role that works. Promote users when they need broader responsibility. Review admins and protected admins regularly. For short-lived access, prefer temporary accounts or temporary roles when that plugin is enabled.
            """),
        ),
        (
            "Admin Guide",
            "bananawiki-admin-guide",
            _doc("""
            # Admin Guide

            Admins manage site settings, users, the built-in documentation, content operations, and plugins.

            ## Site basics

            In **Admin -> Site Settings** you can change the site name, theme colors, default theme, favicon, timezone, interface language, signup behavior, maintenance mode, and many feature defaults. Most changes apply immediately.

            ## Users and access

            Use **Admin -> Users** to create accounts, reset passwords, change roles, suspend users, assign custom roles, and tune per-user permissions. Invite codes can limit who signs up. Session limits can prevent one account from being used in multiple places at the same time.

            ## Documentation lifecycle

            The built-in documentation can be spawned as a BananaWiki category. You can choose full or simplified docs, and English or Italian. Downloading the docs as a ZIP gives you Markdown files that can be edited locally and re-imported with Bulk Markdown Import.

            ## Content operations

            Admins can manage categories, restore or permanently remove slowed deletions, run bulk Markdown imports, export pages, generate PDFs, and use migration tools for full-site backup and restore.

            A full-site export contains every account's password hash, and a full-site import replaces every account. Both ask for your password again and are recorded in the log.

            ## Owners and trust

            Owner status stops other admins from demoting or deleting an account through the normal admin pages. It does not stop an admin who imports a full-site backup or installs a plugin: either one gives complete control of the wiki, including every account. Give the admin role only to people you trust with everything.

            ## Plugins

            Plugins control optional features. Enable only what your wiki will use, then revisit the list as the team grows. Disabling a plugin hides or stops its feature without necessarily deleting stored data.

            ## Recommended routine

            - review new users and admin accounts
            - check recent audit entries for unusual actions
            - keep backups current and occasionally test restore
            - prune old pages or mark outdated content clearly
            - update this documentation when your local workflow differs from the default
            """),
        ),
        (
            "Chat & Messaging",
            "bananawiki-chat-messaging",
            _doc("""
            # Chat & Messaging

            Chat keeps discussion inside the wiki. Depending on site settings and plugins, users can send direct messages, join group chats, attach files, and moderate conversations.

            ## Direct messages

            Direct messages are useful for quick clarification, review requests, and coordination that does not need a full page. If a decision becomes important later, copy the outcome into a wiki page.

            ## Group chats

            Groups can be used for teams, classes, projects, or support queues. Admins can manage membership and moderation settings. Groups work best when each one has a clear purpose and a small set of expected participants.

            ## Attachments and limits

            Chat attachments follow site upload rules. Admins can configure file size limits, daily quotas, message length, and cleanup schedules. If chat is disabled for a user, they should still be able to use the rest of the wiki normally.

            ## Good practice

            Use chat for conversation and pages for durable knowledge. When a thread settles a policy, process, or answer, turn it into documentation so it does not disappear into history.
            """),
        ),
        (
            "Kanban Boards",
            "bananawiki-kanban-boards",
            _doc("""
            # Kanban Boards

            Kanban boards help teams track work without leaving the wiki. A board contains columns, and columns contain tickets. Tickets can represent tasks, ideas, bugs, lessons, or review items.

            ## Create a useful board

            Start with a few columns such as **Backlog**, **Doing**, **Review**, and **Done**. Add more only when they reflect a real workflow. Too many columns make the board harder to scan.

            ## Tickets

            A ticket should have a short title and enough detail for the next person to act. Use comments for discussion and attachments for supporting files. Link to wiki pages when a ticket depends on documented knowledge.

            ## Access and sharing

            Admins configure global Kanban access. Board owners can share individual boards with specific users when the site allows it. Use write access carefully when a board represents operational work.

            ## History and export

            Board history shows how work changed over time. Export features can move board data into backups or external workflows when needed.
            """),
        ),
        (
            "Canvas Layouts",
            "bananawiki-canvas",
            _doc("""
            # Canvas Layouts

            Canvas layouts are visual maps for ideas that do not fit neatly into a linear page. Use them for architecture sketches, concept maps, lesson plans, dependency diagrams, and planning spaces.

            ## Nodes and links

            A canvas is made of nodes connected by edges. Nodes can contain text, references, or structured information depending on enabled features. Link related wiki pages so the canvas becomes an entry point into deeper documentation.

            ## When to use Canvas

            Use a page when the reader needs a clear explanation from top to bottom. Use a canvas when the reader needs relationships: systems, people, dependencies, steps, or alternatives.

            ## Permissions

            Canvas access can be configured globally and shared per layout. View access and edit access may differ. Admins should keep sensitive diagrams under the same category or user restrictions as the pages they describe.

            ## Maintenance

            Visual maps age quickly. Add dates, owners, or links to source pages so readers know whether a diagram is current.
            """),
        ),
        (
            "Badges & Achievements",
            "bananawiki-badges",
            _doc("""
            # Badges & Achievements

            Badges recognize participation. They can be awarded manually by admins or automatically by rules such as contribution counts, account age, or other activity triggers.

            ## Why use badges

            Badges work best as light encouragement: welcoming new contributors, recognizing helpful edits, or marking special responsibilities.

            ## Manual awards

            Admins can award a badge directly to a user. Use manual badges for milestones, training completion, community recognition, or roles that should be visible on profiles.

            ## Automatic awards

            Automatic badges are rule-driven. Keep rules understandable so users know what the badge means. Review auto-award settings after changing plugins or contribution workflows.

            ## Notifications and history

            Badge notifications tell users when they receive a badge. Badge history helps admins audit when and why awards happened.
            """),
        ),
        (
            "Plugins & Extensions",
            "bananawiki-plugins",
            _doc("""
            # Plugins & Extensions

            Plugins let BananaWiki grow without forcing every wiki to use every feature. Built-in plugins cover collaboration, content governance, automation, integrations, and experimental tools.

            ## Managing plugins

            Open **Admin -> Plugins** to enable, disable, inspect, import, or remove plugin packages. Built-in plugins are shipped with BananaWiki. External `.bwplugin` packages can be imported when your deployment allows it.

            ## What plugins can add

            A plugin may add routes, templates, static files, permissions, hooks, database tables, template slots, API behavior, or background jobs. Some plugins are simple UI features; others are substantial subsystems.

            ## Enable with intent

            Enable the tools your team is ready to use. Each feature adds surface area, permissions, and support questions. Start small, then add plugins as workflows become clear.

            ## Data and disabling

            Disabling a plugin normally hides or stops the feature but does not guarantee its stored data is deleted. Re-enabling may bring the data back. Delete or migrate data deliberately when retiring a feature.

            ## Building plugins

            Developers can use the BananaWiki SDK to register hooks, permissions, template slots, and database helpers. Keep plugin behavior narrow, documented, and reversible so admins can understand the impact before enabling it.
            """),
        ),
        (
            "Developer API Reference",
            "bananawiki-api-reference",
            _doc("""
            # Developer API Reference

            BananaWiki exposes developer surfaces for automation, plugins, and integrations. Some endpoints use the web session and CSRF token. The API Service plugin adds Bearer-token endpoints for external clients.

            ## Session-based endpoints

            Common web endpoints include search, Markdown preview, draft management, sidebar filtering, page reservations, Kanban reordering, Canvas data, accessibility preferences, and uploads. Session endpoints require a logged-in user and mutation requests require CSRF protection.

            ## API Service plugin

            When enabled, API Service provides `/api/v1/` endpoints authenticated with Bearer tokens. Users can create personal tokens where allowed. Admins can configure scopes, token limits, expiry behavior, audit visibility, and Banana Mode automation.

            ## Scopes

            Token scopes limit what an integration can do. Prefer the smallest useful scope set: pages for content automation, categories for navigation automation, users for administration, settings for site configuration, and userbot for account automation workflows.

            ## What a token can do

            A token acts as the person who owns it and can never do more than that person could in the editor. Category read and write restrictions apply, so a page outside the owner's readable categories is reported as not found and a write outside their writable categories is refused with `403`. A page that is protected, or checked out by another editor, is refused with `409`. The list of pages only contains what the owner can read. Creating, renaming and moving categories needs the same category permissions as in the wiki.

            Deleting a page through the API follows the same rules as the Delete button. When Deletion Slowdown is on, the page enters its grace period and the request returns `202` with `"pending_deletion": true` instead of removing it.

            ## Plugin SDK

            Plugin authors can use decorators for authentication, register new permissions, add template slots, call safe database helpers, and react to hooks. Treat the SDK as the stable integration layer instead of importing private internals.

            ## Safety checklist

            - keep tokens secret and rotate them when people leave
            - set expiry dates for temporary integrations
            - log and review automation that changes content
            - validate incoming data even when a route is admin-only
            """),
        ),
        (
            "Security & Backups",
            "bananawiki-security",
            _doc("""
            # Security & Backups

            BananaWiki is built for private knowledge, so it protects accounts and content, logs admin and security events, and supports full-site backup and restore.

            ## Account protection

            Passwords are stored as hashes, login attempts are rate-limited, CSRF tokens protect forms and AJAX mutations, and admins can enforce session limits. Suspensions, forced password changes, invite codes, and maintenance mode provide additional controls.

            ## Content protection

            Markdown is sanitized before display. Uploads are validated and dangerous file types are blocked where appropriate. Attachments are served through authenticated routes rather than as public files. Category restrictions and permissions keep sensitive pages away from users who should not see them.

            ## Operational safety

            Audit logs record important administrative and security events. Deletion slowdown can add a grace period before destructive page removal. Page history and backups give admins a path back when content changes unexpectedly.

            ## Backups

            Use full-site exports or deployment backups. A backup is only useful if restore works, so test restoration before an emergency. Keep copies outside the server that hosts the live wiki.

            A full-site export holds every password hash, so store it like a password. Export and import both ask for the admin's password again and are logged. Importing a backup gives complete control of the wiki, owner accounts included, because the backup replaces every account. Installing a plugin does the same, since its code runs with the wiki's own rights.

            ## Production recommendations

            Run behind HTTPS, keep the application updated, limit admin accounts, review plugins before enabling them, monitor disk space, and document your local recovery procedure in the wiki itself.
            """),
        ),
    ]

def _simplified_docs():
    """Return the simplified documentation pages (EN) as a list of (title, slug, content) tuples."""
    return [
        (
            "Welcome to BananaWiki",
            "bananawiki-welcome",
            _doc("""
            # Welcome to BananaWiki

            BananaWiki is a private wiki for your team. It runs on your server, keeps pages in one searchable place, and gives admins control over users, roles, plugins, and backups.

            ## Three things to try first

            1. **Read** - open pages from the sidebar and use search when you know a keyword.
            2. **Write** - create a page when useful knowledge is missing.
            3. **Organize** - move related pages into categories so others can find them later.

            ## What BananaWiki can include

            - Markdown pages with live preview
            - categories and sidebar navigation
            - users, editors, admins, and custom roles
            - optional chat, Kanban, Canvas, badges, attachments, and API tools
            - PDF export, Markdown import, backups, and restore

            ## Who can do what

            | Role | Main purpose |
            |---|---|
            | **User** | read and use allowed collaboration tools |
            | **Editor** | create and update pages |
            | **Admin** | manage the wiki, users, settings, and plugins |
            | **Protected admin** | admin access protected from regular admin changes |

            Admins can recreate this guide from **Admin -> Site Settings -> Wiki Documentation** and choose English or Italian, full or simplified.
            """),
        ),
        (
            "Writing Pages",
            "bananawiki-writing-pages",
            _doc("""
            # Writing Pages

            Pages are written in Markdown. Markdown is plain text with a few symbols for structure and formatting.

            ## Quick reference

            | Write | Result |
            |---|---|
            | `# Title` | large heading |
            | `## Section` | section heading |
            | `**bold**` | bold text |
            | `*italic*` | italic text |
            | `` `code` `` | inline code |
            | `- item` | bullet list |
            | `1. item` | numbered list |
            | `[label](url)` | link |
            | `![alt](url)` | image |

            ## A good page

            A good page has a clear title, a short opening answer, headings for the main sections, and links to related pages. Save a rough page now and improve it later. A useful draft is better than knowledge that stays in chat or memory.

            ## Editing flow

            Create or open a page, write on the left, check the preview on the right, then save. Drafts, page history, attachments, and PDF export may be available depending on enabled plugins and permissions.
            """),
        ),
        (
            "For Admins",
            "bananawiki-for-admins",
            _doc("""
            # For Admins

            Admins shape how the wiki behaves. Most controls live in **Admin -> Site Settings**, **Admin -> Users**, and **Admin -> Plugins**.

            ## First checks

            - confirm the site name, theme, timezone, and language
            - decide whether signup needs invite codes
            - create or invite the first users
            - choose which plugins are useful now
            - make a backup before major imports or migrations

            ## Users and permissions

            Use roles for broad access and category restrictions for where people can work. Use custom roles when several people need the same special access. Keep protected admin accounts limited.

            ## Built-in documentation

            The **Wiki Documentation** section can spawn this guide into the wiki. Choose full or simplified docs, and English or Italian. You can also download the Markdown ZIP, edit it, and import the customized pages.

            ## Backups

            Use the migration export or deployment backups. Managed installations can also use encrypted backups in a private GitHub or Forgejo repository; see `docs/backups.md` in the application source. Telegram backup delivery is retired. Test restore before you rely on a backup strategy.
            """),
        ),
    ]

# The Italian set is written natively rather than translated from the English
# above, so the two are kept slug-for-slug aligned but not sentence-for-sentence.

def _full_docs_it():
    """Return the full documentation pages (IT, native) as a list of (title, slug, content) tuples."""
    return [
        (
            "Benvenuto in BananaWiki",
            "bananawiki-welcome",
            _doc("""
            # Benvenuto in BananaWiki

            BananaWiki è una knowledge base privata e self-hosted per team che vogliono tenere note, guide, piani e memoria operativa in un luogo controllato. È pensata per partire in modo semplice, ma offre anche permessi, plugin, import, export e automazioni quando il progetto cresce.

            ## Cosa puoi mettere nella wiki

            Usa BananaWiki per tutto ciò che le persone chiedono spesso o usano senza accorgersene:

            - onboarding, policy, checklist e runbook
            - piani di progetto, verbali, note di rilascio e decisioni
            - tutorial interni, riferimenti e guide pratiche
            - directory del team, responsabilità e processi
            - pianificazione leggera con bacheche Kanban e diagrammi Canvas

            ## Come è organizzata

            Le pagine contengono testo Markdown. Le categorie organizzano le pagine nella barra laterale. Ruoli e permessi decidono chi può leggere, modificare, amministrare o usare funzioni specifiche. I plugin aggiungono strumenti opzionali come chat, badge, allegati, API, account temporanei e sintesi vocale.

            ## Per iniziare

            1. Esplora la barra laterale e apri una pagina.
            2. Usa la ricerca quando ricordi una parola del titolo o del contenuto.
            3. Crea una pagina quando manca una conoscenza utile, anche se all'inizio è solo una bozza.
            4. Sposta le pagine correlate in categorie chiare.
            5. Chiedi a un admin il ruolo o l'accesso di categoria corretto se qualcosa non è visibile.

            ## Informazioni su questa guida

            Questa documentazione viene generata da BananaWiki. Gli admin possono ricrearla da **Admin -> Impostazioni sito -> Documentazione Wiki**, scaricarla come Markdown, personalizzarla e reimportarla nella wiki.
            """),
        ),
        (
            "Pagine e Modifica",
            "bananawiki-pages-editing",
            _doc("""
            # Pagine e Modifica

            Le pagine sono l'unità principale di BananaWiki. Una buona pagina ha un titolo chiaro, uno slug URL stabile, una categoria utile e un contenuto che risponde a una domanda reale.

            ## Creare una pagina

            1. Clicca **+ Nuova pagina** nella barra laterale o nel menu pagina.
            2. Inserisci un titolo breve. BananaWiki propone automaticamente lo slug URL.
            3. Scegli una categoria, oppure lascia la pagina senza categoria mentre lavori.
            4. Scrivi in Markdown a sinistra e controlla l'anteprima a destra.
            5. Salva quando la pagina è pronta per essere condivisa.

            Per creare e modificare la maggior parte delle pagine serve il ruolo editor o admin. Gli admin possono anche limitare la modifica a categorie specifiche.

            ## Markdown essenziale

            | Scrivi | Risultato |
            |---|---|
            | `# Titolo` | titolo principale |
            | `## Titolo` | titolo di sezione |
            | `**grassetto**` | testo in grassetto |
            | `*corsivo*` | testo in corsivo |
            | `` `codice` `` | codice in linea |
            | `- voce` | elenco puntato |
            | `1. voce` | elenco numerato |
            | `[etichetta](https://example.com)` | link |
            | `![testo alt](immagine.png)` | immagine |

            Usa i titoli per rendere le pagine lunghe facili da scorrere. Metti la risposta più importante in alto e i dettagli sotto.

            ## Media e contenuti ricchi

            Le immagini possono essere caricate dall'editor. I link YouTube o Vimeo su una riga separata vengono incorporati automaticamente. Se il plugin Allegati è attivo, file come PDF o ZIP possono essere collegati alle pagine e scaricati dagli utenti autenticati.

            ## Bozze, cronologia e revisione

            Con il plugin Bozze, le modifiche non salvate restano private mentre lavori. Con Cronologia pagine, ogni salvataggio registra una versione che può essere confrontata, ripristinata o controllata in seguito.

            ## Spostare ed eliminare

            Gli slug possono essere rinominati e i link interni vengono aggiornati dove possibile. Le pagine possono essere spostate tra categorie. L'eliminazione può essere immediata o rallentata dal plugin Rallentamento eliminazioni, a seconda delle impostazioni e del tipo di pagina.
            """),
        ),
        (
            "Categorie e Navigazione",
            "bananawiki-categories-navigation",
            _doc("""
            # Categorie e Navigazione

            Le categorie sono la struttura a cartelle della wiki. Modellano la barra laterale, raggruppano pagine correlate e aiutano i lettori a capire dove cercare.

            ## Usare la barra laterale

            - Clicca una categoria per aprirla o chiuderla.
            - Clicca il titolo di una pagina per aprirla.
            - Usa il campo di ricerca laterale per filtrare rapidamente pagine e categorie.
            - Trascina pagine e categorie se hai il permesso di riordinarle.

            ## Creare categorie utili

            Scegli nomi che corrispondono a come le persone cercano le informazioni: **Operations**, **Prodotto**, **HR**, **Appunti del corso**, **Processo di rilascio** o **Supporto clienti**. Evita annidamenti profondi se non rendono davvero più chiara la navigazione.

            ## Navigazione sequenziale

            La navigazione sequenziale aggiunge link Precedente e Successivo tra le pagine di una categoria. È utile per tutorial, percorsi formativi, manuali e capitoli. L'ordine segue quello impostato nella barra laterale.

            ## Permessi di categoria

            Gli admin possono limitare visibilità e modifica per categoria. Le restrizioni in lettura nascondono contenuti agli utenti non autorizzati. Le restrizioni in scrittura permettono a un editor di lavorare solo in alcune aree.

            ## Eliminare una categoria

            Quando elimini una categoria, scegli cosa fare delle pagine: spostarle, lasciarle senza categoria o eliminarle. Per contenuti importanti, sposta prima e cancella solo dopo aver verificato il risultato.
            """),
        ),
        (
            "Ruoli e Permessi",
            "bananawiki-roles-permissions",
            _doc("""
            # Ruoli e Permessi

            I permessi decidono cosa può vedere e fare ogni persona. BananaWiki parte da ruoli semplici e permette agli admin di aggiungere precisione quando serve.

            ## Ruoli integrati

            | Ruolo | Accesso tipico |
            |---|---|
            | **Utente** | leggere pagine, cercare, usare strumenti collaborativi consentiti |
            | **Editor** | creare e modificare pagine e categorie |
            | **Admin** | gestire utenti, impostazioni, plugin e comportamento del sito |
            | **Admin protetto** | accesso admin non modificabile dagli admin normali |

            ## Ruoli personalizzati

            I ruoli personalizzati combinano un ruolo base con permessi specifici e regole sulle categorie. Usali quando un ruolo integrato è troppo ampio, per esempio **Editor supporto**, **Assistente corso** o **Consulente sola lettura**.

            ## Override per utente

            Gli admin possono sovrascrivere i permessi di un singolo account. È comodo per eccezioni temporanee, ma i ruoli personalizzati sono più facili da mantenere quando più persone hanno bisogno dello stesso accesso.

            ## Restrizioni di categoria

            I permessi rispondono a *cosa può fare questa persona?* Le restrizioni di categoria rispondono a *dove può farlo?* Combinandoli puoi permettere a un utente di modificare un'area e leggere soltanto un'altra.

            ## Consiglio pratico

            Parti dal ruolo più piccolo che funziona. Aumenta l'accesso quando c'è una responsabilità reale. Rivedi periodicamente admin e admin protetti. Per accessi brevi, preferisci account o ruoli temporanei quando il plugin è attivo.
            """),
        ),
        (
            "Guida amministratore",
            "bananawiki-admin-guide",
            _doc("""
            # Guida amministratore

            Gli admin gestiscono impostazioni del sito, utenti, documentazione integrata, operazioni sui contenuti e plugin.

            ## Impostazioni di base

            In **Admin -> Impostazioni sito** puoi cambiare nome del sito, colori, tema predefinito, favicon, fuso orario, lingua dell'interfaccia, iscrizioni, manutenzione e molti valori predefiniti. La maggior parte delle modifiche è immediata.

            ## Utenti e accessi

            Usa **Admin -> Utenti** per creare account, reimpostare password, cambiare ruoli, sospendere utenti, assegnare ruoli personalizzati e regolare permessi. I codici invito limitano le iscrizioni. I limiti di sessione evitano che lo stesso account venga usato in più posti contemporaneamente.

            ## Ciclo di vita della documentazione

            La documentazione integrata può essere creata come categoria BananaWiki. Puoi scegliere guida completa o semplificata, in inglese o italiano. Il download ZIP produce file Markdown modificabili e reimportabili con Importazione Markdown in blocco.

            ## Operazioni sui contenuti

            Gli admin possono gestire categorie, ripristinare o rimuovere eliminazioni rallentate, importare Markdown in blocco, esportare pagine, generare PDF e usare gli strumenti di migrazione per backup e ripristino completi.

            Un'esportazione completa del sito contiene l'hash della password di ogni account, e un'importazione completa sostituisce tutti gli account. Entrambe chiedono di nuovo la tua password e vengono registrate nel log.

            ## Proprietari e fiducia

            Il ruolo di proprietario impedisce agli altri admin di retrocedere o eliminare l'account dalle normali pagine di amministrazione. Non ferma però un admin che importa un backup completo del sito o installa un plugin: in entrambi i casi ottiene il controllo completo della wiki, compresi tutti gli account. Assegna il ruolo di admin solo a persone di cui ti fidi del tutto.

            ## Plugin

            I plugin controllano funzioni opzionali. Attiva solo ciò che la tua wiki usa davvero e rivedi la lista quando il team cresce. Disattivare un plugin nasconde o ferma la funzione, ma non elimina necessariamente i dati già salvati.

            ## Routine consigliata

            - controlla nuovi utenti e account admin
            - leggi gli audit recenti per azioni insolite
            - mantieni backup aggiornati e prova il ripristino
            - archivia o segnala i contenuti obsoleti
            - aggiorna questa documentazione quando il workflow locale cambia
            """),
        ),
        (
            "Chat e Messaggistica",
            "bananawiki-chat-messaging",
            _doc("""
            # Chat e Messaggistica

            La chat tiene le discussioni dentro la wiki. In base alle impostazioni e ai plugin, gli utenti possono inviare messaggi diretti, partecipare a chat di gruppo, allegare file e moderare conversazioni.

            ## Messaggi diretti

            I messaggi diretti servono per chiarimenti veloci, richieste di revisione e coordinamento che non richiede una pagina. Se una decisione diventa importante, copia il risultato in una pagina wiki.

            ## Chat di gruppo

            I gruppi possono rappresentare team, classi, progetti o code di supporto. Gli admin gestiscono membri e moderazione. I gruppi funzionano meglio quando hanno uno scopo chiaro e partecipanti attesi.

            ## Allegati e limiti

            Gli allegati seguono le regole di upload del sito. Gli admin possono configurare dimensioni massime, quote giornaliere, lunghezza messaggi e pulizie automatiche. Se la chat è disabilitata per un utente, il resto della wiki dovrebbe continuare a funzionare normalmente.

            ## Buona pratica

            Usa la chat per conversare e le pagine per la conoscenza duratura. Quando una discussione stabilisce una policy, un processo o una risposta, trasformala in documentazione.
            """),
        ),
        (
            "Bacheche Kanban",
            "bananawiki-kanban-boards",
            _doc("""
            # Bacheche Kanban

            Le bacheche Kanban aiutano a seguire il lavoro senza uscire dalla wiki. Una bacheca contiene colonne e le colonne contengono ticket. I ticket possono rappresentare task, idee, bug, lezioni o revisioni.

            ## Creare una bacheca utile

            Parti con poche colonne, per esempio **Backlog**, **In corso**, **Revisione** e **Fatto**. Aggiungine altre solo se rispecchiano davvero il workflow. Troppe colonne rendono la bacheca più difficile da leggere.

            ## Ticket

            Un ticket dovrebbe avere un titolo breve e dettagli sufficienti per agire. Usa i commenti per la discussione e gli allegati per i file di supporto. Collega pagine wiki quando il ticket dipende da conoscenza documentata.

            ## Accesso e condivisione

            Gli admin configurano l'accesso Kanban globale. I proprietari possono condividere singole bacheche con utenti specifici quando il sito lo consente. Usa l'accesso in scrittura con attenzione quando la bacheca rappresenta lavoro operativo.

            ## Cronologia ed export

            La cronologia mostra come il lavoro è cambiato nel tempo. Le funzioni di export possono spostare i dati della bacheca in backup o workflow esterni.
            """),
        ),
        (
            "Layout Canvas",
            "bananawiki-canvas",
            _doc("""
            # Layout Canvas

            I Canvas sono mappe visive per idee che non entrano bene in una pagina lineare. Usali per architetture, mappe concettuali, lezioni, dipendenze e spazi di pianificazione.

            ## Nodi e collegamenti

            Un canvas è fatto di nodi collegati da linee. I nodi possono contenere testo, riferimenti o informazioni strutturate a seconda delle funzioni abilitate. Collega pagine wiki correlate perché il canvas diventi una porta d'ingresso alla documentazione.

            ## Quando usare Canvas

            Usa una pagina quando il lettore ha bisogno di una spiegazione dall'inizio alla fine. Usa un canvas quando servono relazioni: sistemi, persone, dipendenze, passaggi o alternative.

            ## Permessi

            L'accesso Canvas può essere configurato globalmente e condiviso per singolo layout. Lettura e modifica possono avere regole diverse. Gli admin dovrebbero proteggere i diagrammi sensibili come le pagine che descrivono.

            ## Manutenzione

            Le mappe visive invecchiano rapidamente. Aggiungi date, proprietari o link alle pagine sorgente così i lettori capiscono se il diagramma è ancora attuale.
            """),
        ),
        (
            "Badge e Traguardi",
            "bananawiki-badges",
            _doc("""
            # Badge e Traguardi

            I badge riconoscono la partecipazione. Possono essere assegnati manualmente dagli admin o automaticamente da regole come numero di contributi, anzianità dell'account o altri trigger.

            ## Perché usarli

            I badge funzionano meglio come incoraggiamento leggero: dare il benvenuto a nuovi contributori, riconoscere modifiche utili o evidenziare responsabilità speciali.

            ## Assegnazioni manuali

            Gli admin possono assegnare direttamente un badge a un utente. Usa i badge manuali per milestone, completamento di formazione, riconoscimenti o ruoli che devono essere visibili nel profilo.

            ## Assegnazioni automatiche

            I badge automatici sono guidati da regole. Mantieni le regole comprensibili così gli utenti sanno cosa significa un badge. Rivedile dopo cambiamenti a plugin o workflow di contribuzione.

            ## Notifiche e cronologia

            Le notifiche avvisano gli utenti quando ricevono un badge. La cronologia aiuta gli admin a controllare quando e perché i badge sono stati assegnati.
            """),
        ),
        (
            "Plugin ed Estensioni",
            "bananawiki-plugins",
            _doc("""
            # Plugin ed Estensioni

            I plugin permettono a BananaWiki di crescere senza obbligare ogni wiki a usare ogni funzione. I plugin integrati coprono collaborazione, governance dei contenuti, automazione, integrazioni e strumenti sperimentali.

            ## Gestire i plugin

            Apri **Admin -> Plugin** per attivare, disattivare, ispezionare, importare o rimuovere pacchetti plugin. I plugin integrati sono distribuiti con BananaWiki. I pacchetti esterni `.bwplugin` possono essere importati quando il deployment lo consente.

            ## Cosa possono aggiungere

            Un plugin può aggiungere route, template, file statici, permessi, hook, tabelle database, slot template, API o lavori in background. Alcuni plugin sono piccole funzioni UI; altri sono sottosistemi completi.

            ## Attiva con intenzione

            Attiva gli strumenti che il team è pronto a usare. Ogni funzione aggiunge superficie, permessi e domande di supporto. Parti con pochi plugin e aggiungine altri quando i workflow sono chiari.

            ## Dati e disattivazione

            Disattivare un plugin di solito nasconde o ferma la funzione, ma non garantisce la cancellazione dei dati. Riattivandolo, i dati potrebbero tornare visibili. Cancella o migra dati con una decisione esplicita quando ritiri una funzione.

            ## Sviluppare plugin

            Gli sviluppatori possono usare il BananaWiki SDK per registrare hook, permessi, slot template e helper database. Mantieni i plugin circoscritti, documentati e reversibili così gli admin capiscono l'impatto prima di abilitarli.
            """),
        ),
        (
            "Riferimento API per sviluppatori",
            "bananawiki-api-reference",
            _doc("""
            # Riferimento API per sviluppatori

            BananaWiki espone superfici per automazione, plugin e integrazioni. Alcuni endpoint usano sessione web e token CSRF. Il plugin API Service aggiunge endpoint Bearer-token per client esterni.

            ## Endpoint basati su sessione

            Gli endpoint web comuni includono ricerca, anteprima Markdown, bozze, filtro sidebar, prenotazioni pagina, riordino Kanban, dati Canvas, preferenze di accessibilità e upload. Richiedono un utente autenticato e le mutazioni richiedono protezione CSRF.

            ## Plugin API Service

            Quando è attivo, API Service fornisce endpoint `/api/v1/` autenticati con Bearer token. Gli utenti possono creare token personali dove consentito. Gli admin configurano scope, limiti, scadenze, audit e automazioni Banana Mode.

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
            """),
        ),
        (
            "Sicurezza e Backup",
            "bananawiki-security",
            _doc("""
            # Sicurezza e Backup

            BananaWiki è progettata per conoscenza privata, quindi protegge account e contenuti, registra gli eventi amministrativi e di sicurezza e permette backup e ripristino completi del sito.

            ## Protezione account

            Le password sono salvate come hash, i tentativi di login sono limitati, i token CSRF proteggono form e mutazioni AJAX, e gli admin possono imporre limiti di sessione. Sospensioni, cambio password obbligatorio, codici invito e modalità manutenzione aggiungono altri controlli.

            ## Protezione contenuti

            Il Markdown viene sanificato prima della visualizzazione. Gli upload sono validati e i tipi pericolosi vengono bloccati dove opportuno. Gli allegati sono serviti da route autenticate, non come file pubblici. Restrizioni di categoria e permessi tengono le pagine sensibili lontane dagli utenti non autorizzati.

            ## Sicurezza operativa

            Gli audit registrano eventi amministrativi e di sicurezza importanti. Il rallentamento eliminazioni può aggiungere un periodo di grazia prima della rimozione definitiva. Cronologia pagina e backup danno agli admin una via di recupero.

            ## Backup

            Usa esportazioni complete o backup del server. Un backup è utile solo se il ripristino funziona, quindi prova il restore prima di un'emergenza. Conserva copie fuori dal server della wiki live.

            Un'esportazione completa contiene l'hash di ogni password, quindi conservala come una password. Esportazione e importazione chiedono di nuovo la password dell'admin e vengono registrate nel log. Importare un backup dà il controllo completo della wiki, account dei proprietari compresi, perché il backup sostituisce tutti gli account. Installare un plugin fa lo stesso, perché il suo codice gira con gli stessi diritti della wiki.

            ## Produzione

            Esegui dietro HTTPS, mantieni l'app aggiornata, limita gli account admin, valuta i plugin prima di abilitarli, monitora lo spazio disco e documenta nella wiki la procedura locale di recupero.
            """),
        ),
    ]

def _simplified_docs_it():
    """Italian version of the simplified docs, written natively (not auto-translated)."""
    return [
        (
            "Benvenuto in BananaWiki",
            "bananawiki-welcome",
            _doc("""
            # Benvenuto in BananaWiki

            BananaWiki è una wiki privata per il tuo team. Gira sul tuo server, tiene le pagine in un luogo ricercabile e dà agli admin controllo su utenti, ruoli, plugin e backup.

            ## Per iniziare

            1. **Leggi** - apri le pagine dalla barra laterale e usa la ricerca quando conosci una parola chiave.
            2. **Scrivi** - crea una pagina quando manca una conoscenza utile.
            3. **Organizza** - sposta le pagine correlate in categorie così gli altri le trovano dopo.

            ## Cosa può includere BananaWiki

            - pagine Markdown con anteprima dal vivo
            - categorie e navigazione laterale
            - utenti, editor, admin e ruoli personalizzati
            - chat, Kanban, Canvas, badge, allegati e API opzionali
            - export PDF, import Markdown, backup e ripristino

            ## Chi può fare cosa

            | Ruolo | Scopo principale |
            |---|---|
            | **Utente** | leggere e usare gli strumenti consentiti |
            | **Editor** | creare e aggiornare pagine |
            | **Admin** | gestire wiki, utenti, impostazioni e plugin |
            | **Admin protetto** | accesso admin protetto da modifiche degli admin normali |

            Gli admin possono ricreare questa guida da **Admin -> Impostazioni sito -> Documentazione Wiki** e scegliere inglese o italiano, completa o semplificata.
            """),
        ),
        (
            "Scrivere pagine",
            "bananawiki-writing-pages",
            _doc("""
            # Scrivere pagine

            Le pagine sono scritte in Markdown. Markdown è testo semplice con pochi simboli per struttura e formattazione.

            ## Riferimento rapido

            | Scrivi | Risultato |
            |---|---|
            | `# Titolo` | titolo grande |
            | `## Sezione` | titolo di sezione |
            | `**grassetto**` | testo in grassetto |
            | `*corsivo*` | testo in corsivo |
            | `` `codice` `` | codice in linea |
            | `- voce` | elenco puntato |
            | `1. voce` | elenco numerato |
            | `[etichetta](url)` | link |
            | `![alt](url)` | immagine |

            ## Una buona pagina

            Una buona pagina ha un titolo chiaro, una risposta breve all'inizio, titoli per le sezioni principali e link alle pagine correlate. Salva subito una prima versione e migliorala dopo. Una bozza utile è meglio di una conoscenza che resta in chat o nella memoria di qualcuno.

            ## Flusso di modifica

            Crea o apri una pagina, scrivi a sinistra, controlla l'anteprima a destra e salva. Bozze, cronologia, allegati ed export PDF possono essere disponibili in base a plugin e permessi.
            """),
        ),
        (
            "Per amministratori",
            "bananawiki-for-admins",
            _doc("""
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

            La sezione **Documentazione Wiki** può creare questa guida dentro la wiki. Scegli guida completa o semplificata, in inglese o italiano. Puoi anche scaricare lo ZIP Markdown, modificarlo e importare le pagine personalizzate.

            ## Backup

            Usa l'export di migrazione o i backup di deployment. Le installazioni gestite supportano anche backup cifrati in un repository privato GitHub o Forgejo; consulta `docs/backups.md` nel codice sorgente. L'invio dei backup su Telegram è stato ritirato. Prova il ripristino prima di affidarti a una strategia di backup.
            """),
        ),
    ]

def _translate_docs_to_italian(pages):
    """Return an Italian-labelled variant of *pages*.

    Both the full and the simplified doc sets have hand-written native
    Italian rewrites.  We pick the right rewrite by comparing the set of
    slugs in *pages* against the slug sets returned by
    ``_full_docs_it()`` and ``_simplified_docs_it()``.

    When the input doesn't match either set exactly we fall back to a
    header-only translation (Italian title, English body) so callers
    passing custom page sets still get something usable.
    """
    page_slugs = {slug for _t, slug, _c in pages}
    full_it = _full_docs_it()
    simplified_it = _simplified_docs_it()
    full_slugs = {slug for _t, slug, _c in full_it}
    simplified_slugs = {slug for _t, slug, _c in simplified_it}

    if page_slugs == simplified_slugs:
        return list(simplified_it)
    if page_slugs == full_slugs:
        return list(full_it)

    # Fallback: title-translation only.  Preserves backwards
    # compatibility for any caller that builds a custom doc set on top
    # of ``_full_docs()`` / ``_simplified_docs()``.
    title_lookup = {}
    for native in (full_it, simplified_it):
        for it_title, slug, _content in native:
            title_lookup[slug] = it_title

    translated = []
    for title, slug, content in pages:
        it_title = title_lookup.get(slug, title)
        content = content or ""
        if content.startswith("# ") and "\n" in content:
            _heading, rest = content.split("\n", 1)
            content = f"# {it_title}\n{rest}"
        elif content.startswith("# "):
            content = f"# {it_title}\n"
        elif content.strip():
            content = f"# {it_title}\n\n{content}"
        else:
            content = f"# {it_title}\n"
        translated.append((it_title, slug, content))
    return translated


# End of the content tables; the rest of the module creates, refreshes and
# removes the doc pages those tables describe.
def _get_docs_category_id():
    """Return the tracked docs category ID from settings, or None."""
    settings = get_site_settings()
    if settings is None:
        return None
    return settings.get("docs_category_id")


def _delete_existing_docs():
    """Delete the existing documentation category and all its pages.

    Bypasses the deletion_slowdown plugin entirely: pages are removed
    immediately regardless of plugin state.
    """
    cat_id = _get_docs_category_id()
    if cat_id is None:
        return
    cat = get_category(cat_id)
    if cat is None:
        # Category was already manually deleted; just clear the setting.
        update_site_settings(docs_category_id=None)
        return
    # Force-delete all pages in the category (bypasses deletion_slowdown)
    with get_db_context() as conn:
        conn.execute("DELETE FROM pages WHERE category_id=? AND is_home=0", (cat_id,))
        conn.commit()
    # Now delete the category itself (pages already gone, uncategorize is harmless)
    delete_category(cat_id, page_action="uncategorize")
    update_site_settings(docs_category_id=None)


def spawn_wiki_docs(user_id, simplified=False, language="en"):
    """Create (or recreate) the BananaWiki documentation category with pages.

    If a documentation category already exists it is deleted first so the
    content is always fresh.

    The full delete-and-recreate flow runs inside a single
    ``BEGIN IMMEDIATE`` transaction, and a process-level lock prevents
    concurrent calls within the same worker from racing.  Together this
    guarantees that two near-simultaneous spawn requests can never produce
    duplicate "BananaWiki" categories or leave the wiki with a partially-
    spawned docs set if one of them errors out.

    Parameters
    ----------
    simplified : bool
        When ``True``, spawns a shorter set of beginner-friendly pages.
    language : str
        Interface language code (``"en"`` or ``"it"``). When ``"it"``,
        the matching native Italian rewrite is used.

    All documentation pages are attributed to the system (not to any user).

    Returns
    -------
    int
        The category ID of the newly-created documentation category.
    """
    # Select and (optionally) translate the page set outside the
    # transaction so the SQLite write lock is held for as little time as
    # possible.
    pages = _simplified_docs() if simplified else _full_docs()
    if (language or "").strip().lower() == "it":
        pages = _translate_docs_to_italian(pages)

    _ = user_id  # Kept for API compatibility; docs are always system-authored.
    with _SPAWN_DOCS_LOCK:
        return _spawn_wiki_docs_atomic(pages)


def _spawn_wiki_docs_atomic(pages):
    """Run the full delete-then-recreate flow inside one transaction.

    Holds the SQLite reserved-write lock for the whole operation via
    ``BEGIN IMMEDIATE`` so that even cross-process / cross-worker spawn
    requests serialise (the second one waits up to ``busy_timeout`` and
    then sees the first one's results when it acquires the lock).

    Returns the category ID of the freshly-created docs category.
    """
    now = datetime.now(timezone.utc).isoformat()
    with get_db_context() as conn:
        try:
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.cursor()

            # Find every existing docs category.  We delete:
            #   1. The category currently tracked in
            #      site_settings.docs_category_id
            #   2. Any category whose name matches DOCS_CATEGORY_NAME
            # Step 2 cleans up duplicates that may have been produced by
            # past races where the categories.name column had no UNIQUE
            # constraint.
            tracked_row = conn.execute(
                "SELECT docs_category_id FROM site_settings WHERE id=1"
            ).fetchone()
            tracked_id = (
                tracked_row["docs_category_id"] if tracked_row else None
            )
            duplicate_rows = conn.execute(
                "SELECT id FROM categories WHERE name=?",
                (DOCS_CATEGORY_NAME,),
            ).fetchall()

            ids_to_remove = {r["id"] for r in duplicate_rows}
            if tracked_id is not None:
                ids_to_remove.add(tracked_id)

            for old_id in ids_to_remove:
                # Delete docs pages (defensively keep any is_home=1 page).
                conn.execute(
                    "DELETE FROM pages WHERE category_id=? AND is_home=0",
                    (old_id,),
                )
                # Detach child categories (mirror delete_category's
                # uncategorize behaviour).
                conn.execute(
                    "UPDATE categories SET parent_id=NULL WHERE parent_id=?",
                    (old_id,),
                )
                # Uncategorize anything left over (e.g. is_home=1).
                conn.execute(
                    "UPDATE pages SET category_id=NULL WHERE category_id=?",
                    (old_id,),
                )
                conn.execute(
                    "DELETE FROM categories WHERE id=?",
                    (old_id,),
                )

            # Create the fresh docs category.
            cur.execute(
                "INSERT INTO categories (name, parent_id) VALUES (?, ?)",
                (DOCS_CATEGORY_NAME, None),
            )
            cat_id = cur.lastrowid

            # Track the new category in site_settings.
            conn.execute(
                "UPDATE site_settings SET docs_category_id=? WHERE id=1",
                (cat_id,),
            )

            # Insert the pages.  sort_order is sequential within the new
            # (empty) category so we don't need to query MAX(sort_order).
            for sort_idx, (title, slug, content) in enumerate(pages):
                cur.execute(
                    "INSERT INTO pages (title, slug, content, category_id, "
                    "last_edited_by, last_edited_at, sort_order) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (title, slug, content, cat_id, SYSTEM_USER_ID, now, sort_idx),
                )
                page_id = cur.lastrowid
                cur.execute(
                    "INSERT INTO page_history "
                    "(page_id, title, content, edited_by, edit_message, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (page_id, title, content, SYSTEM_USER_ID, "Page created", now),
                )

            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.execute("PRAGMA foreign_keys=ON")

    return cat_id


def is_docs_category(category_id):
    """Return True if *category_id* is the tracked documentation category."""
    if category_id is None:
        return False
    settings = get_site_settings()
    if settings is None:
        return False
    docs_cat = settings.get("docs_category_id")
    return docs_cat is not None and int(docs_cat) == int(category_id)


def get_docs_pages(simplified=False, language="en"):
    """Return the list of (title, slug, content) tuples for a doc set.

    This is the public hook used by ``spawn_wiki_docs`` and by tooling
    such as ``scripts/sync_user_guide_docs.py`` to keep the on-disk
    Markdown reference in ``docs/user-guide/`` aligned with the in-app
    spawn content.
    """
    pages = _simplified_docs() if simplified else _full_docs()
    if (language or "").strip().lower() == "it":
        pages = _translate_docs_to_italian(pages)
    return list(pages)


def build_docs_archive(simplified=False, language="en"):
    """Return ``(zip_bytes, filename)`` containing the spawn-able docs.

    The archive layout mirrors what :func:`spawn_wiki_docs` lays down in
    the wiki: a top-level ``BananaWiki/`` folder with one Markdown file
    per page.  The archive is directly re-importable through the
    existing **Admin → Site Settings → Bulk Markdown Import** flow, so
    admins can customise the docs locally and upload an edited bundle
    for a wiki-specific guide.

    Parameters mirror :func:`spawn_wiki_docs`.
    """
    import io
    import zipfile

    pages = get_docs_pages(simplified=simplified, language=language)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # Make the archive deterministic so identical inputs hash the
        # same (helps audit / diff tooling).
        for title, slug, content in pages:
            # Sanitise the file name so re-zipping on any OS works.
            safe = "".join(
                c if c.isalnum() or c in "-_" else "-" for c in (slug or title)
            ).strip("-") or "page"
            arcname = f"{DOCS_CATEGORY_NAME}/{safe}.md"
            info = zipfile.ZipInfo(arcname)
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, content or "")

    suffix = "simplified" if simplified else "full"
    lang = "it" if (language or "").strip().lower() == "it" else "en"
    filename = f"bananawiki-docs-{suffix}-{lang}.zip"
    return buf.getvalue(), filename
