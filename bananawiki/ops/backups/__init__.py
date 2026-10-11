"""Optional encrypted off-site backups in a private GitHub or Forgejo repository.

A port of 1.4 ``banana_backup`` with the same on-repository format, so
snapshots uploaded by 1.4 restore with 1.6 and the other way round:

* each snapshot is a root commit on ``refs/heads/banana-backups/bananawiki/<series>/<id>``
  holding ``index.json`` and ``part-NNNNN.age`` (age-encrypted, 32 MiB parts);
* the plaintext starts with ``BananaSuite backup v1`` and a JSON header binding
  the product, series and snapshot id, followed by a ``banana`` package;
* ``.../<series>/identity`` pins the recovery key's recipient to the series;
* ``index.json`` also carries ``authentication``: an HMAC-SHA256,
  keyed from the recovery identity, over the rest of the index (context and
  the SHA-256 of every part). Restores refuse snapshots without it unless
  ``--allow-unauthenticated``; older releases ignore the extra key;
* ``repository.json`` may set ``append_only``: uploads then never delete
  snapshots (the token needs no right to delete branches) and ``backups
  prune`` applies the retention with a token given for that command only.

Configuration lives in ``config/remote-backup/`` (repository.json,
schedule.json, repo.token, recovery.agekey, last-backup.json).
"""
