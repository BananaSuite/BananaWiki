# Sicherheit und Backups

Wie das Wiki Konten und Inhalte schützt und wie du es sicherst.

## Schutz der Konten

Passwörter werden als Hashes gespeichert, Anmeldeversuche sind begrenzt, und Formulare sind gegen Anfragen von fremden Websites geschützt. Administratoren können Konten sperren, eine Passwortänderung erzwingen, Einladungscodes verlangen, nur eine Sitzung pro Konto erlauben und das Wiki in den Wartungsmodus versetzen.

Alle können unter **Einstellungen → Bestätigung in zwei Schritten** die **Bestätigung in zwei Schritten** einschalten: Nach dem Passwort fragt die Anmeldung dann zusätzlich nach einem Code aus einer Authenticator-App auf dem Handy oder nach einem von zehn einmalig nutzbaren Wiederherstellungscodes, die beim Einschalten angezeigt werden. Eigentümer und Superuser können sie unter **Verwaltung → Website-Einstellungen → Sicherheit** für alle Administratoren verlangen und sie unter **Verwaltung → Benutzer** für Personen zurücksetzen, die sowohl ihr Handy als auch ihre Wiederherstellungscodes verloren haben.

## Schutz der Inhalte

Markdown wird vor der Anzeige von unsicherem HTML bereinigt. Uploads werden gegen die Größenlimits und die erlaubten und gesperrten Dateitypen geprüft, und unbewegte Bilder werden neu kodiert, wodurch Standortdaten entfernt werden. Anhänge werden nur an Personen ausgeliefert, die die Seite lesen dürfen.

## Änderungen rückgängig machen

Das Audit-Protokoll erfasst Anmeldungen, Änderungen an Konten und Rollen, Löschungen und Änderungen an Einstellungen. Der Versionsverlauf stellt frühere Fassungen wieder her, und die Löschverzögerung bewahrt gelöschte Seiten 48 Stunden lang auf.

## Backups

Sichere mit dem Export der gesamten Website oder mit den Backups des Servers, bewahre Kopien auf einem anderen Rechner auf und teste eine Wiederherstellung, bevor du sie brauchst.

Ein Export der gesamten Website enthält alle Passwort-Hashes. Bewahre ihn deshalb so sicher auf wie ein Passwort. Nur Eigentümer und Superuser können die gesamte Website exportieren oder importieren; beides verlangt erneut das Passwort und wird protokolliert. Wer ein Backup importiert, erhält die volle Kontrolle über das Wiki, einschließlich der Eigentümerkonten, denn das Backup ersetzt alle Konten. Dasselbe gilt für das Installieren eines Plugins, da sein Code mit den Berechtigungen des Wikis selbst läuft.
