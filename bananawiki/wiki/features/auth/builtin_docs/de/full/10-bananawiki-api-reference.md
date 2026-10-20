# REST-API

Das Plugin **REST-API** fügt JSON-Endpunkte unter `/api/v1/` für Skripte und Integrationen hinzu. Anfragen tragen ein Bearer-Token. Die vollständige Liste der Endpunkte steht unter `/api-docs` in deinem Wiki. Die Endpunkte unter `/api/`, die die Weboberfläche nutzt, sind intern und können sich zwischen Versionen ändern.

## Einschalten

Ein Administrator aktiviert das Plugin unter **Verwaltung → Plugins** und setzt dann auf dessen Einstellungsseite den Haken bei *REST-API aktivieren*; dort werden auch die Anfragelimits und die Zahl der Tokens pro Konto festgelegt.

## Tokens

Jedes Konto erstellt seine Tokens unter **Einstellungen → API-Tokens**. Ein Token wird nur einmal angezeigt; widerrufe Tokens, die du nicht mehr brauchst.

## Bereiche

Ein Token erreicht nur die Endpunkte seiner Bereiche: `pages`, `categories`, `kanban`, `canvas`, `tokens` und für Administratoren `users`, `settings` und `admin`. `userbot` ist für den Automatisierungsschlüssel.

## Was ein Token darf

Ein Token handelt im Namen der Person, der es gehört, und kann nie mehr tun als diese Person im Editor. Lese- und Schreibbeschränkungen für Kategorien gelten auch hier: Eine Seite außerhalb der Kategorien, die die Person lesen darf, wird als nicht gefunden gemeldet, und ein Schreibzugriff außerhalb der Kategorien, in denen sie schreiben darf, wird mit `403` abgelehnt. Eine geschützte oder von jemand anderem reservierte Seite wird mit `409` abgelehnt. Die Seitenliste enthält nur Seiten, die die Person lesen darf. Kategorien anlegen, umbenennen und verschieben erfordert dieselben Kategorieberechtigungen wie im Wiki.

Eine Seite über die API zu löschen folgt denselben Regeln wie die Schaltfläche Löschen. Wenn die Löschverzögerung eingeschaltet ist, beginnt für die Seite die Wartezeit, und die Anfrage antwortet mit `202` und `"pending_deletion": true`, statt die Seite zu entfernen.
