# Plugins & Extensions

Plugins let BananaWiki grow without forcing every wiki to use every feature. Built-in plugins cover collaboration, content governance, automation, integrations, and experimental tools.

## Managing plugins

Open **Administration → Plugins** to enable, disable, inspect, import, or remove plugin packages. Built-in plugins are shipped with BananaWiki. External `.bwplugin` packages run with the wiki's full rights, so they are off unless the server operator allows them (`BW_ALLOW_EXTERNAL_PLUGINS=1`), and only the owner or a superuser can import or enable one. Other administrators can still switch plugins off and remove them.

## What plugins can add

A plugin may add routes, templates, static files, permissions, hooks, database tables, template slots, API behaviour, or background jobs. Some plugins are simple UI features; others are substantial subsystems.

## Enable with intent

Enable the tools your team is ready to use. Each feature adds surface area, permissions, and support questions. Start small, then add plugins as workflows become clear.

## Data and disabling

Disabling a plugin normally hides or stops the feature but does not guarantee its stored data is deleted. Re-enabling may bring the data back. Delete or migrate data deliberately when retiring a feature.

## Building plugins

Developers can use the BananaWiki SDK to register hooks, permissions, template slots, and database helpers. Keep plugin behaviour narrow, documented, and reversible so admins can understand the impact before enabling it.
