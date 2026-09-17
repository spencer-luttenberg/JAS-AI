# Jarrett AI

A small Discord bot with `!ping` and `!hello` commands.

## Setup

1. Create and activate a virtual environment:

   ```powershell
   py -3.13 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

2. Install the dependency:

   ```powershell
   python -m pip install -r requirements.txt
   ```

3. In the Discord Developer Portal, open the bot's settings and enable the
   **Message Content Intent** under **Privileged Gateway Intents**.

4. Set the regenerated bot token for the current PowerShell session and run
   the bot:

   ```powershell
   $env:DISCORD_TOKEN="YOUR_REGENERATED_TOKEN"
   python bot.py
   ```

## Commands

- `!ping` replies with `Pong!`
- `!hello` mentions the user who ran the command

Keep the bot token out of source files, commits, screenshots, and chat
messages. If a token is exposed, reset it immediately in the Discord Developer
Portal and replace any deployed copy.
