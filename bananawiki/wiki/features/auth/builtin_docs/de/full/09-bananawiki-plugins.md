# Plugins

Die meisten Funktionen von BananaWiki sind Plugins, die ein Administrator ein- oder ausschaltet.

## Plugins verwalten

Öffne **Verwaltung → Plugins**, um Plugin-Pakete zu aktivieren, zu deaktivieren, zu prüfen, zu importieren oder zu entfernen. Integrierte Plugins werden mit BananaWiki ausgeliefert. Externe `.bwplugin`-Pakete laufen mit allen Rechten des Wikis. Deshalb sind sie ausgeschaltet, solange der Betreiber des Servers sie nicht erlaubt (`BW_ALLOW_EXTERNAL_PLUGINS=1`), und nur der Eigentümer oder ein Superuser kann eines importieren oder aktivieren. Andere Administratoren können Plugins trotzdem ausschalten und entfernen.

## Ein Plugin deaktivieren

Ein deaktiviertes Plugin blendet seine Seiten und Menüs aus. Seine Daten und Einstellungen bleiben erhalten und kommen zurück, wenn du es wieder aktivierst.

## Plugins entwickeln

Ein Plugin meldet eine Funktion genauso an wie die integrierten; siehe `docs/plugins/README.md` im Quellcode. Plugins, die für 1.4 mit `bananawiki_sdk` geschrieben wurden, werden weiterhin geladen.
