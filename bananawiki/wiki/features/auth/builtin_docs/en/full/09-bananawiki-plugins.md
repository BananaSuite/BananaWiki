# Plugins

Most features of BananaWiki are plugins that an administrator switches on or off.

## Managing plugins

Open **Administration → Plugins** to enable, disable, inspect, import or remove plugin packages. Built-in plugins are shipped with BananaWiki. External `.bwplugin` packages run with the wiki's full rights, so they are off unless the server operator allows them (`BW_ALLOW_EXTERNAL_PLUGINS=1`), and only the owner or a superuser can import or enable one. Other administrators can still switch plugins off and remove them.

## Disabling a plugin

Disabling a plugin hides its pages and menus. Its data and settings stay, and come back when you enable it again.

## Building plugins

A plugin declares a feature in the same way as the built-in ones; see `docs/plugins/README.md` in the source. Plugins written for 1.4 with `bananawiki_sdk` still load.
