# Jarrett AI

Jarrett AI is a Discord bot with OpenAI-powered answers and PostgreSQL-backed
long-term memory. It records every human message and its own replies in the
Discord channels you approve, imports accessible older history, and searches
that archive when someone mentions `@JAS AI` with a question.

This tutorial covers the complete setup for Discord, OpenAI, Railway, and
PostgreSQL.

## Before You Start

You need:

- A Discord application with a bot user
- The bot installed in your Discord server
- A regenerated Discord bot token that has never been shared publicly
- An OpenAI API key
- A Railway project containing the Python bot service
- This repository connected to the Railway bot service
- Python 3.13 if you also want to run the bot locally

Never put a real token, API key, or database URL in this repository.

## 1. Configure the Discord Application

### Enable Message Content Intent

The bot cannot record or search message text without this intent.

1. Open the [Discord Developer Portal](https://discord.com/developers/applications).
2. Select your Jarrett AI application.
3. Select **Bot** in the left sidebar.
4. Find **Privileged Gateway Intents**.
5. Turn on **Message Content Intent**.
6. Save the changes if Discord displays a save button.

If the bot token has ever been posted in chat, committed, or shown in a
screenshot, select **Reset Token** on the Bot page and use only the new token.

### Grant Discord Channel Permissions

For every channel whose history Jarrett AI should remember, the bot needs:

- **View Channel**
- **Read Message History**
- **Send Messages**

To configure an individual channel:

1. In Discord, right-click the channel and select **Edit Channel**.
2. Select **Permissions**.
3. Add the bot or its bot role if it is not already listed.
4. Allow **View Channel**, **Read Message History**, and **Send Messages**.
5. Save the changes.

Repeat this for each recorded channel. The bot cannot backfill messages it does
not have permission to view.

### Copy the Channel IDs

1. Open Discord **User Settings**.
2. Select **Advanced**.
3. Turn on **Developer Mode**.
4. Close Settings.
5. Right-click a channel that Jarrett AI should remember.
6. Select **Copy Channel ID**.
7. Save the ID somewhere temporarily.
8. Repeat for every channel that should be searchable.

The final value will be a comma-separated list with no quotes:

```text
123456789012345678,234567890123456789
```

New messages in threads below an approved channel are recorded automatically.
To import the older history of an existing thread, right-click the thread,
copy its own ID, and add that ID to the list too.

## 2. Create an OpenAI API Key

1. Open the [OpenAI API key page](https://platform.openai.com/api-keys).
2. Sign in to the OpenAI Platform account that will own the bot's API usage.
3. Create a new secret API key.
4. Copy the key when it is shown.
5. Store it in a password manager until it has been added to Railway.

Do not put the key in `bot.py`, `.env` files committed to Git, README examples,
Discord messages, or screenshots. The OpenAI SDK reads `OPENAI_API_KEY` from
the environment.

## 3. Create PostgreSQL on Railway

Skip this section if the PostgreSQL service already exists in the same Railway
project as the bot.

1. Open the Jarrett AI project in Railway.
2. On the Project Canvas, select **+ New**.
3. Select **Database**.
4. Select **PostgreSQL**.
5. Wait for the PostgreSQL service to finish provisioning.
6. Note the service's exact name. Railway commonly names it `Postgres`, but
   your service may have a different name.

Do not manually create tables. When the bot starts, it automatically applies
the SQL files in `database/migrations/` and creates:

- `discord_messages`
- `discord_channel_sync_state`
- The indexes used for chronological and full-text history searches

## 4. Connect the Bot Service to PostgreSQL

These variables belong on the Railway **bot service**, not on the PostgreSQL
service.

1. On the Railway Project Canvas, select the Python bot service.
2. Open its **Variables** tab.
3. Add the following variables.

| Variable name | Value |
| --- | --- |
| `DISCORD_TOKEN` | Your regenerated Discord bot token |
| `OPENAI_API_KEY` | Your OpenAI API key |
| `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` |
| `LISTEN_CHANNEL_IDS` | Your comma-separated Discord channel IDs |
| `OPENAI_MODEL` | Optional; defaults to `gpt-5.6-luna` |

For `DATABASE_URL`, replace `Postgres` with the exact PostgreSQL service name.
For example, if the database service is named `JarrettPostgres`, use:

```text
${{JarrettPostgres.DATABASE_URL}}
```

Railway's variable editor should offer autocomplete when you begin entering a
reference value. Select the PostgreSQL service and its `DATABASE_URL` variable
from that list.

Use the private `DATABASE_URL` reference inside Railway. Do not paste the
database password or use `DATABASE_PUBLIC_URL` for communication between two
services in the same Railway project.

Example `LISTEN_CHANNEL_IDS` value:

```text
123456789012345678,234567890123456789
```

Do not add spaces, `#` channel-name prefixes, or quotes.

## 5. Configure and Deploy the Bot Service

Railway normally detects the Python start command automatically. To set it
explicitly:

1. Select the Railway bot service.
2. Open **Settings**.
3. Find the deployment **Start Command** setting.
4. Enter:

   ```text
   python bot.py
   ```

5. Apply the staged variable/settings changes or select **Redeploy**.

Railway installs `requirements.txt`, starts `bot.py`, connects to PostgreSQL,
and applies both database migrations automatically.

## 6. Watch the First Deployment

Open the bot service's deployment logs. A successful startup should include
messages similar to:

```text
Applying database migration 001_create_discord_messages.sql
Applying database migration 002_add_long_term_search.sql
Logged in as Jarrett AI
Connected to 1 server(s)
Recording 2 configured channel(s)
Starting full backfill for Discord channel 123456789012345678
Finished full backfill for channel 123456789012345678; stored 850 message(s)
```

The first deployment imports all accessible history for every ID in
`LISTEN_CHANNEL_IDS`. Large channels can take a while. Discord rate limits are
handled by `discord.py`; leave the deployment running until every configured
channel reports `Finished full backfill`.

Later restarts use catch-up mode and fetch only messages newer than the last
successful sync:

```text
Starting catch-up for Discord channel 123456789012345678
Finished catch-up for channel 123456789012345678; stored 12 message(s)
```

The bot remains online while the background history import runs, but its
long-term answers are most complete after the first backfill finishes.

## 7. Test the Bot in Discord

In an approved channel, send:

```text
!ping
```

The bot should answer:

```text
Pong!
```

Then test OpenAI. Type `@JAS AI`, select the bot from Discord's autocomplete so
it becomes a real mention, and add the question after it:

```text
@JAS AI Summarize what we have discussed about camera repairs.
```

After the backfill completes, ask about an older topic using a few distinctive
words that appeared in the original conversation:

```text
@JAS AI What did we decide about guards repairing disabled cameras?
```

Typing the bot's name as plain text is not enough; Discord must render it as a
clickable mention. The `!ask` command remains available as a fallback.

Every new human message in an approved channel is stored immediately, even
when nobody uses a bot command. Ordinary message ingestion does not call
OpenAI. OpenAI is called only when the bot is mentioned with a question or
someone uses `!ask`.

## 8. Verify the PostgreSQL Data

The most reliable command-line method is Railway CLI plus PostgreSQL's `psql`
client.

### Install and Connect

1. Install [PostgreSQL command-line tools](https://www.postgresql.org/download/windows/)
   so the `psql` command is available.
2. Install Railway CLI. This command requires Node.js 16 or newer:

   ```powershell
   npm install -g @railway/cli
   ```

3. From this repository directory, authenticate and link the project:

   ```powershell
   railway login
   railway link
   ```

4. Connect to PostgreSQL, replacing `Postgres` with its exact Railway service
   name:

   ```powershell
   railway connect Postgres
   ```

### Run Verification Queries

At the `psql` prompt, check recent messages:

```sql
SELECT author_name, content, created_at
FROM discord_messages
ORDER BY created_at DESC
LIMIT 20;
```

Check how much history was imported and its date range:

```sql
SELECT
    COUNT(*) AS messages,
    MIN(created_at) AS oldest_message,
    MAX(created_at) AS newest_message
FROM discord_messages;
```

Check backfill status for each configured channel:

```sql
SELECT channel_id, last_message_id, backfilled_at, updated_at
FROM discord_channel_sync_state
ORDER BY channel_id;
```

Exit `psql` with:

```text
\q
```

## 9. Run Locally (Optional)

Railway deployment is the normal always-on setup. Local testing requires a
database URL reachable from your computer.

### Make Railway PostgreSQL Reachable Locally

1. Select the PostgreSQL service in Railway.
2. Open **Settings**.
3. Open **Networking**.
4. Add **Public Access**.
5. Railway creates a `DATABASE_PUBLIC_URL` variable.
6. Use that public URL only as the local `DATABASE_URL` value.

Public database access is not required for the deployed bot. Remove it after
local testing if you do not otherwise need it.

### Start the Bot in PowerShell

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt

$env:DISCORD_TOKEN="YOUR_REGENERATED_DISCORD_TOKEN"
$env:OPENAI_API_KEY="YOUR_OPENAI_API_KEY"
$env:DATABASE_URL="YOUR_RAILWAY_DATABASE_PUBLIC_URL"
$env:LISTEN_CHANNEL_IDS="123456789012345678,234567890123456789"

python bot.py
```

PowerShell `$env:` values apply only to the current terminal session. Opening a
new terminal requires setting them again.

## 10. Add Another Channel Later

1. Give the bot **View Channel**, **Read Message History**, and **Send
   Messages** permissions in the new channel.
2. Copy the new channel ID.
3. Append it to the Railway bot service's `LISTEN_CHANNEL_IDS` variable.
4. Redeploy the bot service.
5. Watch the logs for the new channel's `Finished full backfill` message.

Existing channel sync state is preserved. Only the newly added channel needs a
full import.

## How Long-Term Memory Works

The bot does not send the entire Discord archive to OpenAI on every question.
That would eventually exceed model input limits and waste API usage.

Instead:

1. Every human message and Jarrett AI reply in approved channels is stored in
   PostgreSQL.
2. PostgreSQL maintains a full-text search index automatically.
3. Mentioning `@JAS AI` with a question searches the complete stored server
   history for relevant messages.
4. The bot also loads the 15 most recent messages from the current channel.
5. Relevant old messages, recent conversation, and the current question are
   sent to OpenAI.
6. The answer is posted to Discord and stored like other Jarrett AI replies.

DMs, messages from other bots, and channels absent from `LISTEN_CHANNEL_IDS`
are not stored. Message edits update the stored row. Discord deletion events
remove the stored row.

Current long-term retrieval uses PostgreSQL keyword and stemming search. It
works best when the question includes names or topic words used in the original
discussion. Semantic vector search can be added later for synonym-heavy
questions.

## Commands

- `@JAS AI <question>` is the primary way to ask a question
- `!ping` replies with `Pong!`
- `!hello` mentions the user who ran the command
- `!ask <question>` provides a command-based fallback

## Troubleshooting

### `DATABASE_URL is required`

`DATABASE_URL` was not added to the Railway bot service, or the variable
reference was not deployed. Add `${{Postgres.DATABASE_URL}}` using the exact
database service name and redeploy.

### The Bot Logs In but Stores Zero Messages

Check all of the following:

- `LISTEN_CHANNEL_IDS` contains numeric channel IDs, not channel names
- The current channel ID is included
- The bot has **View Channel** and **Read Message History**
- **Message Content Intent** is enabled in the Discord Developer Portal
- The bot service was redeployed after changing Railway variables

### Discord Closes the Connection with Code `4014`

The code is requesting Message Content Intent, but the intent is not enabled
for the application. Enable it under **Bot > Privileged Gateway Intents** in
the Discord Developer Portal, then restart the bot.

### Backfill Reports `Cannot Read History`

Grant **View Channel** and **Read Message History** for that channel. Confirm
the ID belongs to a channel in a server where the bot is installed.

### A Mention or `!ask` Says It Cannot Reach OpenAI

Check that:

- `OPENAI_API_KEY` is valid and belongs to an API project
- The OpenAI API account can make API requests
- `OPENAI_MODEL`, if set, names a model available to that project
- The Railway bot service was redeployed after changing the key

### Railway Cannot Determine How to Start the Service

Set the bot service's Start Command to:

```text
python bot.py
```

### Local Python Imports Fail

Confirm the virtual environment is active and reinstall dependencies:

```powershell
.\.venv\Scripts\Activate.ps1
python --version
python -m pip install -r requirements.txt
python -m pip check
```

## Security and Privacy

- Never commit or share `DISCORD_TOKEN`, `OPENAI_API_KEY`, `DATABASE_URL`, or
  `DATABASE_PUBLIC_URL`.
- Reset any secret immediately if it is exposed.
- Only record channels whose members know their messages are being retained.
- Review your retention policy before storing private or sensitive discussion.
- Keep PostgreSQL private unless public access is temporarily needed for local
  tools.

## Official Documentation

- [Discord privileged intents](https://discord.com/developers/docs/events/gateway#privileged-intents)
- [OpenAI API quickstart](https://developers.openai.com/api/docs/quickstart/)
- [Railway PostgreSQL](https://docs.railway.com/databases/postgresql)
- [Railway reference variables](https://docs.railway.com/variables#referencing-another-services-variable)
- [Railway start commands](https://docs.railway.com/deployments/start-command)
- [Railway CLI database connection](https://docs.railway.com/cli/connect)
- [PostgreSQL full-text search](https://www.postgresql.org/docs/current/textsearch-tables.html)
