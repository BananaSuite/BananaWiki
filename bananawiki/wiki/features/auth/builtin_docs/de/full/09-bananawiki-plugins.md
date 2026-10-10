# Plugins und Erweiterungen

Mit Plugins kann BananaWiki wachsen, ohne dass jedes Wiki jede Funktion nutzen muss. Integrierte Plugins decken Zusammenarbeit, Regeln für Inhalte, Automatisierung, Integrationen und experimentelle Werkzeuge ab.

## Plugins verwalten

Öffne **Verwaltung -> Plugins**, um Plugin-Pakete zu aktivieren, zu deaktivieren, zu prüfen, zu importieren oder zu entfernen. Integrierte Plugins werden mit BananaWiki ausgeliefert. Externe `.bwplugin`-Pakete kannst du importieren, wenn deine Installation das erlaubt.

## Was Plugins hinzufügen können

Ein Plugin kann Routen, Templates, statische Dateien, Berechtigungen, Hooks, Datenbanktabellen, Template-Slots, API-Verhalten oder Hintergrundaufträge hinzufügen. Manche Plugins sind einfache Funktionen der Oberfläche, andere sind umfangreiche Subsysteme.

## Mit Bedacht aktivieren

Aktiviere nur die Werkzeuge, für die dein Team bereit ist. Jede Funktion macht das System umfangreicher und bringt zusätzliche Berechtigungen und Supportfragen mit sich. Beginne klein und füge Plugins hinzu, sobald die Arbeitsabläufe klar sind.

## Daten und Deaktivierung

Wenn du ein Plugin deaktivierst, wird die Funktion in der Regel ausgeblendet oder angehalten, ihre gespeicherten Daten werden aber nicht zwingend gelöscht. Wenn du das Plugin wieder aktivierst, können die Daten wieder erscheinen. Lösche oder migriere Daten bewusst, wenn du eine Funktion einstellst.

## Plugins entwickeln

Wer Plugins entwickelt, kann mit dem BananaWiki-SDK Hooks, Berechtigungen, Template-Slots und Datenbank-Hilfsfunktionen registrieren. Halte das Verhalten eines Plugins eng begrenzt, dokumentiert und umkehrbar, damit Administratoren die Auswirkungen verstehen, bevor sie das Plugin aktivieren.
