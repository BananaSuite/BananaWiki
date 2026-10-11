# Anleitung für Administratoren

Administratoren verwalten Website-Einstellungen, Konten, die integrierte Dokumentation, Inhalte und Plugins.

## Grundeinstellungen der Website

Unter **Verwaltung → Website-Einstellungen** kannst du den Namen der Website, die Farben des Designs, das Standarddesign, das Website-Symbol, die Zeitzone, die Oberflächensprache, die Regeln für die Registrierung, den Wartungsmodus und viele Standardwerte für Funktionen ändern. Die meisten Änderungen gelten sofort.

## Konten und Zugriff

Unter **Verwaltung → Benutzer** kannst du Konten erstellen, Passwörter zurücksetzen, Rollen ändern, Konten sperren, eigene Rollen zuweisen und Berechtigungen für einzelne Konten anpassen. Mit Einladungscodes kannst du einschränken, wer sich registrieren darf. Sitzungslimits können verhindern, dass ein Konto gleichzeitig an mehreren Orten genutzt wird. Eigentümer und Superuser können die Bestätigung in zwei Schritten für Administratoren verlangen und sie für ein Konto zurücksetzen, das seine Authenticator-App und seine Wiederherstellungscodes verloren hat.

## Lebenszyklus der Dokumentation

Die integrierte Dokumentation kann unter **Verwaltung → Dokumentation** als BananaWiki-Kategorie angelegt werden. Du kannst zwischen der ausführlichen Anleitung und der Kurzanleitung wählen, jeweils auf Deutsch, Englisch oder Italienisch. Wenn du die Dokumentation als ZIP-Archiv herunterlädst, erhältst du Markdown-Dateien, die du lokal bearbeiten und über den Markdown-Massenimport wieder einspielen kannst.

## Arbeiten mit Inhalten

Administratoren können Kategorien verwalten, Seiten mit verzögerter Löschung wiederherstellen oder endgültig löschen, Markdown-Massenimporte durchführen, Seiten exportieren, PDFs erzeugen und mit den Werkzeugen für den Website-Umzug die gesamte Website sichern und wiederherstellen.

Ein vollständiger Export der Website enthält den Passwort-Hash jedes Kontos, und ein vollständiger Import ersetzt alle Konten. Deshalb können nur Eigentümer und Superuser sie ausführen; bei beiden musst du dein Passwort erneut eingeben, und beide werden im Protokoll festgehalten. Bei Massenlöschungen musst du zur Bestätigung LÖSCHEN eingeben.

## Eigentümer und Vertrauen

Der Eigentümerstatus verhindert, dass andere Administratoren ein Konto über die normalen Verwaltungsseiten herabstufen oder löschen. Wer ein vollständiges Backup der Website importiert oder ein Drittanbieter-Plugin installiert, erhält die volle Kontrolle über das Wiki, einschließlich aller Konten. Deshalb können das nur Eigentümer und Superuser. Andere Administratoren verwalten trotzdem alle übrigen Konten und alle Inhalte: Gib die Administratorrolle nur Personen, denen du das anvertraust, und mache jemanden nur dann zum Eigentümer oder Superuser, wenn du dieser Person in jeder Hinsicht vertraust.

## Plugins

Plugins steuern optionale Funktionen. Aktiviere nur, was dein Wiki tatsächlich nutzt, und überprüfe die Liste erneut, wenn das Team wächst. Wenn du ein Plugin deaktivierst, wird seine Funktion ausgeblendet oder angehalten, ohne dass gespeicherte Daten zwangsläufig gelöscht werden.

## Empfohlene Routine

- neue Konten und Administratorkonten überprüfen
- die letzten Einträge im Audit-Protokoll auf ungewöhnliche Aktionen prüfen
- Backups aktuell halten und die Wiederherstellung gelegentlich testen
- alte Seiten aufräumen oder veraltete Inhalte deutlich kennzeichnen
- diese Dokumentation anpassen, wenn die Abläufe in deinem Wiki vom Standard abweichen
