# Anleitung für Administratoren

Administratoren verwalten Website-Einstellungen, Konten, die integrierte Dokumentation, Inhalte und Plugins.

## Grundeinstellungen der Website

Unter **Verwaltung -> Website-Einstellungen** kannst du den Namen der Website, die Farben des Designs, das Standarddesign, das Website-Symbol, die Zeitzone, die Oberflächensprache, die Regeln für die Registrierung, den Wartungsmodus und viele Standardwerte für Funktionen ändern. Die meisten Änderungen gelten sofort.

## Konten und Zugriff

Unter **Verwaltung -> Benutzer** kannst du Konten erstellen, Passwörter zurücksetzen, Rollen ändern, Konten sperren, eigene Rollen zuweisen und Berechtigungen für einzelne Konten anpassen. Mit Einladungscodes kannst du einschränken, wer sich registrieren darf. Sitzungslimits können verhindern, dass ein Konto gleichzeitig an mehreren Orten genutzt wird.

## Lebenszyklus der Dokumentation

Die integrierte Dokumentation kann als BananaWiki-Kategorie angelegt werden. Du kannst zwischen der ausführlichen Anleitung und der Kurzanleitung wählen, jeweils auf Deutsch, Englisch oder Italienisch. Wenn du die Dokumentation als ZIP-Archiv herunterlädst, erhältst du Markdown-Dateien, die du lokal bearbeiten und über den Markdown-Massenimport wieder einspielen kannst.

## Arbeiten mit Inhalten

Administratoren können Kategorien verwalten, Seiten mit verzögerter Löschung wiederherstellen oder endgültig löschen, Markdown-Massenimporte durchführen, Seiten exportieren, PDFs erzeugen und mit den Werkzeugen für den Website-Umzug die gesamte Website sichern und wiederherstellen.

Ein vollständiger Export der Website enthält den Passwort-Hash jedes Kontos, und ein vollständiger Import ersetzt alle Konten. Bei beiden musst du dein Passwort erneut eingeben, und beide werden im Protokoll festgehalten.

## Eigentümer und Vertrauen

Der Eigentümerstatus verhindert, dass andere Administratoren ein Konto über die normalen Verwaltungsseiten herabstufen oder löschen. Er schützt aber nicht vor einem Administrator, der ein vollständiges Backup der Website importiert oder ein Plugin installiert: Beides verschafft die volle Kontrolle über das Wiki, einschließlich aller Konten. Gib die Administratorrolle nur Personen, denen du in jeder Hinsicht vertraust.

## Plugins

Plugins steuern optionale Funktionen. Aktiviere nur, was dein Wiki tatsächlich nutzt, und überprüfe die Liste erneut, wenn das Team wächst. Wenn du ein Plugin deaktivierst, wird seine Funktion ausgeblendet oder angehalten, ohne dass gespeicherte Daten zwangsläufig gelöscht werden.

## Empfohlene Routine

- neue Konten und Administratorkonten überprüfen
- die letzten Einträge im Audit-Protokoll auf ungewöhnliche Aktionen prüfen
- Backups aktuell halten und die Wiederherstellung gelegentlich testen
- alte Seiten aufräumen oder veraltete Inhalte deutlich kennzeichnen
- diese Dokumentation anpassen, wenn die Abläufe in deinem Wiki vom Standard abweichen
