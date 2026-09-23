# Web-first onboarding

Server registration does not happen in the Linux terminal.

1. The installer deploys infrastructure and generates a one-time setup token.
2. The owner opens the Telegram WebApp and creates the root business with that token.
3. The owner opens **Servers → Add server** and enters the PasarGuard HTTPS URL and API key.
4. The API verifies connectivity before saving anything.
5. Credentials are encrypted with the application master key and are never returned to the browser.
6. Nodes are loaded live from PasarGuard and reconnect commands are issued from the WebApp.

The terminal is limited to infrastructure installation, diagnostics, backups and updates.
