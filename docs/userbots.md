# Userbot Automation

BananaWiki supports **Userbot accounts** through the built-in **API Service**
plugin. Users can enable automation mode in **Settings -> API Tokens** and
receive an API key for:

- `GET /api/v1/userbot/me`
- `POST /api/v1/userbot/profile`

When automation mode is enabled:

- the profile shows an **Automated Account** badge,
- the API key is active,
- admins can review status, lock mode, and enable/disable counts.

When automation mode is disabled:

- the profile automation badge is removed,
- the API key is revoked.

## Python SDK

Use the built-in SDK package:

```python
from bananawiki_userbot_sdk import UserbotClient

client = UserbotClient("https://wiki.example.com", "YOUR_USERBOT_API_KEY")
print(client.me())
client.update_profile(real_name="Bot User", bio="Automated profile update", page_published=True)
```
