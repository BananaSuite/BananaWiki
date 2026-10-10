# API-Referenz für Entwickler

BananaWiki stellt Entwicklungsschnittstellen für Automatisierung, Plugins und Integrationen bereit. Einige Endpunkte nutzen die Web-Sitzung und das CSRF-Token. Das Plugin API Service ergänzt Endpunkte mit Bearer-Token für externe Clients.

## Sitzungsbasierte Endpunkte

Zu den üblichen Web-Endpunkten gehören Suche, Markdown-Vorschau, Verwaltung von Entwürfen, Filtern der Seitenleiste, Seitenreservierungen, Neuordnen in Kanban-Boards, Canvas-Daten, Einstellungen zur Barrierefreiheit und Uploads. Sitzungsendpunkte erfordern eine angemeldete Person, und Anfragen, die Daten ändern, brauchen CSRF-Schutz.

## Plugin API Service

Ist API Service aktiviert, stellt das Plugin Endpunkte unter `/api/v1/` bereit, die per Bearer-Token authentifiziert werden. Personen können eigene Tokens erstellen, wo das erlaubt ist. Administratoren können Zugriffsbereiche, Token-Limits, das Ablaufverhalten und die Sichtbarkeit im Audit-Protokoll einstellen.

## Zugriffsbereiche

Zugriffsbereiche begrenzen, was eine Integration tun darf. Wähle möglichst wenige, aber ausreichende Bereiche: pages für die Automatisierung von Inhalten, categories für die Automatisierung der Navigation, users für die Verwaltung, settings für die Website-Einstellungen und userbot für automatisierte Abläufe mit Konten.

## Was ein Token darf

Ein Token handelt im Namen der Person, der es gehört, und kann nie mehr tun, als diese Person im Editor könnte. Lese- und Schreibbeschränkungen für Kategorien gelten auch hier: Eine Seite außerhalb der Kategorien, die diese Person lesen darf, wird als nicht gefunden gemeldet, und ein Schreibzugriff außerhalb der Kategorien, in denen sie schreiben darf, wird mit `403` abgelehnt. Eine Seite, die geschützt oder von einer anderen Person reserviert ist, wird mit `409` abgelehnt. Die Seitenliste enthält nur, was diese Person lesen darf. Wer Kategorien erstellen, umbenennen oder verschieben will, braucht dieselben Kategorieberechtigungen wie im Wiki.

Das Löschen einer Seite über die API folgt denselben Regeln wie die Schaltfläche „Löschen“. Ist die Löschverzögerung aktiv, beginnt für die Seite die Wartezeit, und die Anfrage liefert `202` mit `"pending_deletion": true`, statt die Seite zu entfernen.

## Plugin-SDK

Wer Plugins entwickelt, kann Dekoratoren für die Authentifizierung verwenden, neue Berechtigungen registrieren, Template-Slots hinzufügen, sichere Datenbank-Hilfsfunktionen aufrufen und auf Hooks reagieren. Betrachte das SDK als stabile Integrationsschicht, statt private Interna zu importieren.

## Sicherheitscheckliste

- halte Tokens geheim und erneuere sie, wenn jemand das Team verlässt
- lege Ablaufdaten für befristete Integrationen fest
- protokolliere und prüfe Automatisierungen, die Inhalte ändern
- validiere eingehende Daten auch dann, wenn eine Route nur Administratoren offensteht
