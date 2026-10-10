# Sicherheit und Backups

BananaWiki ist für internes Wissen gebaut. Deshalb schützt es Konten und Inhalte, protokolliert Verwaltungs- und Sicherheitsereignisse und unterstützt Backup und Wiederherstellung der gesamten Website.

## Schutz der Konten

Passwörter werden als Hashes gespeichert, die Zahl der Anmeldeversuche pro Zeitraum ist begrenzt, CSRF-Tokens schützen Formulare und AJAX-Änderungen, und Administratoren können Sitzungslimits festlegen. Sperren, erzwungene Passwortänderungen, Einladungscodes und der Wartungsmodus bieten weitere Steuerungsmöglichkeiten.

## Schutz der Inhalte

Markdown wird vor der Anzeige bereinigt. Uploads werden geprüft, und gefährliche Dateitypen werden, wo sinnvoll, blockiert. Anhänge werden über authentifizierte Routen ausgeliefert, nicht als öffentliche Dateien. Kategoriebeschränkungen und Berechtigungen halten vertrauliche Seiten von Personen fern, die sie nicht sehen sollen.

## Sicherheit im Betrieb

Audit-Protokolle erfassen wichtige Verwaltungs- und Sicherheitsereignisse. Die Löschverzögerung kann eine Wartezeit einfügen, bevor Seiten endgültig entfernt werden. Versionsverlauf und Backups geben Administratoren einen Weg zurück, wenn sich Inhalte unerwartet ändern.

## Backups

Nutze Exporte der gesamten Website oder Backups deiner Installation. Ein Backup nützt nur, wenn die Wiederherstellung funktioniert. Teste die Wiederherstellung deshalb vor einem Notfall. Bewahre Kopien außerhalb des Servers auf, auf dem das laufende Wiki gehostet wird.

Ein Export der gesamten Website enthält alle Passwort-Hashes. Bewahre ihn deshalb so sicher auf wie ein Passwort. Export und Import verlangen beide erneut das Passwort des Administrators und werden protokolliert. Wer ein Backup importiert, erhält die volle Kontrolle über das Wiki, einschließlich der Eigentümerkonten, denn das Backup ersetzt alle Konten. Dasselbe gilt für das Installieren eines Plugins, da sein Code mit den Berechtigungen des Wikis selbst läuft.

## Empfehlungen für den Produktivbetrieb

Betreibe das Wiki nur über HTTPS, halte die Anwendung aktuell, beschränke die Zahl der Administratorkonten, prüfe Plugins, bevor du sie aktivierst, überwache den Speicherplatz und dokumentiere dein lokales Wiederherstellungsverfahren im Wiki selbst.
