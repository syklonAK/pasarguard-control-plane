# PasarGuard B2B Control Plane

Enterprise wholesale and reseller management for PasarGuard with usage billing, recursive reseller accounts, encrypted server credentials, Telegram Bot and Telegram WebApp.

## Product flow

1. Install the infrastructure with one command.
2. Open the Telegram bot and tap **Open Panel**.
3. Enter the one-time setup token shown after installation.
4. Register PasarGuard servers from **WebApp → Servers**.
5. Manage resellers, nodes, credit and billing from the WebApp. Server registration is not performed in the Linux terminal.

## One-command installation

Requirements: Ubuntu 22.04/24.04 or Debian 12, a domain pointing to the server, ports 80/443, and a Telegram bot token.

```bash
curl -fsSL https://raw.githubusercontent.com/syklonAK/pasarguard-control-plane/main/install.sh | sudo bash
```

All installer prompts and messages are in English. Existing `.env` secrets are preserved when the installer is rerun.

## First-time WebApp setup

After installation, open the bot and tap **Open Panel**. Read the one-time token with:

```bash
sudo grep INITIAL_SETUP_TOKEN /root/pasarguard-control-plane-credentials.txt
```

Use the WebApp onboarding screen to create the root business. Then add PasarGuard panels from **Servers → Add server**. API keys and owner credentials are encrypted before database storage.

## Updates

Do not reinstall. Run the local updater:

```bash
sudo bash /opt/pasarguard-control-plane/update.sh
```

It preserves `.env`, pulls the latest release, validates Compose, rebuilds changed services, runs tracked database migrations, checks the internal API and refreshes Telegram configuration.

## Recover a previously interrupted installation

```bash
cd /opt/pasarguard-control-plane
git pull --ff-only
sudo bash update.sh
```

## Operations

```bash
sudo bash /opt/pasarguard-control-plane/scripts/doctor.sh
sudo bash /opt/pasarguard-control-plane/scripts/backup.sh
sudo bash /opt/pasarguard-control-plane/update.sh
```

## Capacity

Defaults are safe for a 2-vCPU pilot server. A real 10,000-user production deployment requires staged load tests and larger PostgreSQL/API capacity. Before accepting real money, complete backup-restore drills, reconciliation, MFA/RBAC and approval workflows.
