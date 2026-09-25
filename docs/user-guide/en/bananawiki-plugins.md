# Plugins & Extensions

Plugins let BananaWiki grow without forcing every wiki to use every feature. Built-in plugins cover collaboration, content governance, automation, integrations, and experimental tools.

## Managing plugins

Open **Admin -> Plugins** to enable, disable, inspect, import, or remove plugin packages. Built-in plugins are shipped with BananaWiki. External `.bwplugin` packages can be imported when your deployment allows it.

## What plugins can add

A plugin may add routes, templates, static files, permissions, hooks, database tables, template slots, API behavior, or background jobs. Some plugins are simple UI features; others are substantial subsystems.

## Enable with intent

Enable the tools your team is ready to use. Each feature adds surface area, permissions, and support questions. Start small, then add plugins as workflows become clear.

## Data and disabling

Disabling a plugin normally hides or stops the feature but does not guarantee its stored data is deleted. Re-enabling may bring the data back. Delete or migrate data deliberately when retiring a feature.

## Building plugins

Developers can use the BananaWiki SDK to register hooks, permissions, template slots, and database helpers. Keep plugin behavior narrow, documented, and reversible so admins can understand the impact before enabling it.
