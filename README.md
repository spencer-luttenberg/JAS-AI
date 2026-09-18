# Jarrett AI

Jarrett AI is a Discord bot with OpenAI-powered answers and PostgreSQL-backed
long-term memory. It records every human message and its own replies in the
Discord channels you approve, imports accessible older history, indexes files
from Google Drive, synchronizes Jira Cloud issues and activity, and searches
those sources when someone mentions `@JAS AI` with a question. Jira writes are
never automatic: a user must confirm each proposed change in Discord.

This tutorial covers the complete setup for Discord, OpenAI, Railway,
PostgreSQL, Google Drive, and Jira Cloud.

## Before You Start

You need:

- A Discord application with a bot user
- The bot installed in your Discord server
- A regenerated Discord bot token that has never been shared publicly
- An OpenAI API key
- A Google account and Google Cloud project for Drive access
- A Jira Cloud site and Atlassian account for Jira integration
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
- `google_drive_files`, `google_drive_chunks`, and Drive sync tracking tables
- `jira_issues`, `jira_comments`, `jira_changes`, and Jira sync/approval tables
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
| `PROJECT_UPDATE_CHANNEL_ID` | Optional; channel for scheduled project reports |
| `PROJECT_UPDATE_INTERVAL_HOURS` | Optional; defaults to `12` |
| `PROJECT_NAME` | Optional; defaults to `Jarrett AI` |
| `PROJECT_WEB_SEARCH_TOPICS` | Optional hints for recurring web research |
| `JIRA_BASE_URL` | Optional; site such as `https://example.atlassian.net` |
| `JIRA_EMAIL` | Optional; email for the Atlassian account that owns the token |
| `JIRA_API_TOKEN` | Optional; Atlassian API token, never an account password |
| `JIRA_PROJECT_KEYS` | Optional; comma-separated project keys such as `JAS,OPS` |
| `JIRA_BOARD_ID` | Required for sprint/story-point changes; numeric board ID |
| `JIRA_DEFAULT_ISSUE_TYPE` | Optional; defaults to `Story` for natural-language creates |
| `JIRA_SYNC_INTERVAL_SECONDS` | Optional; defaults to `600`, minimum `300` |

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

## 5. Configure Google Drive Reading

Google Drive access is optional. Without the variables in this section, the
bot continues to work with Discord history only.

This setup uses a read-only Google service account. For a personal **My
Drive**, put everything the bot should read beneath one top-level folder and
share that folder with the service account. Access is inherited by its
subfolders. For a Google Workspace **Shared Drive**, add the service account as
a member and use the Shared Drive root folder ID.

### Create the Google Service Account

1. Open the [Google Cloud Console](https://console.cloud.google.com/).
2. Create a project or select the project you want to use for Jarrett AI.
3. Open **APIs & Services > Library**.
4. Search for **Google Drive API**, open it, and select **Enable**.
5. Open **IAM & Admin > Service Accounts**.
6. Select **Create service account**.
7. Name it `jas-ai-drive-reader`, then finish creating it. It does not need a
   Google Cloud IAM role because file access is granted from Google Drive.
8. Open the new service account and copy its email address. It ends in
   `iam.gserviceaccount.com`.
9. Open the service account's **Keys** tab.
10. Select **Add key > Create new key > JSON > Create**.
11. Keep the downloaded JSON file private. It contains a credential that can
    act as the service account.

### Share the Drive Content

For a personal My Drive:

1. Create or choose one top-level folder containing everything Jarrett AI may
   read.
2. Right-click that folder and select **Share**.
3. Add the service-account email as a **Viewer**.
4. Open the folder and copy its URL. The folder ID is the value after
   `/folders/`; the bot accepts either the ID or the complete folder URL.

For a Shared Drive, add the service-account email as a member with at least
**Viewer** access and copy the URL of the Shared Drive's root folder.

Only share content that Discord users in your approved channels are allowed to
retrieve. The bot only supplies Drive results inside channels configured in
`LISTEN_CHANNEL_IDS`.

### Encode the Credential for Railway

In PowerShell, replace the example path with the downloaded JSON file path:

```powershell
[Convert]::ToBase64String(
    [IO.File]::ReadAllBytes("$HOME\Downloads\jas-ai-drive-reader.json")
) | Set-Clipboard
```

This puts the base64-encoded credential on the clipboard. It does not print
the secret in the terminal. Add these variables to the Railway **JAS-AI bot
service**:

| Variable name | Value |
| --- | --- |
| `GOOGLE_SERVICE_ACCOUNT_JSON_BASE64` | Paste the base64 value from the clipboard |
| `GOOGLE_DRIVE_FOLDER_IDS` | The shared folder ID or complete folder URL |
| `GOOGLE_DRIVE_SYNC_INTERVAL_SECONDS` | Optional; defaults to `3600` (one hour) |

To index multiple folders, separate their IDs with commas. The minimum sync
interval is five minutes. After storing the credential securely in Railway,
remove the downloaded JSON key from locations where it is no longer needed.

The indexer reads:

- Google Docs, Sheets, Slides, and Drawings
- PDF, DOCX, XLSX, and PPTX files
- Plain text, Markdown, CSV, TSV, JSON, XML, YAML, logs, and source text

Images, audio, video, Google Forms, shortcuts, legacy Office formats, and
other unsupported binaries are skipped. Image-only or scanned PDFs need OCR
before their text becomes searchable.

## 6. Configure Jira Cloud

Jira is optional. If its four required variables are absent, the Discord,
Drive, and project-update features continue to work. This integration supports
**Jira Cloud**, not Jira Server or Jira Data Center.

### Create a Dedicated Jira Account and Permissions

A dedicated Atlassian account is recommended because API-token access has the
same Jira permissions as the account that created it.

1. Create or choose an Atlassian account for Jarrett AI and give it Jira product
   access.
2. Add that account to each project Jarrett AI should read or manage.
3. Grant **Browse Projects** so it can synchronize issues, comments, and change
   history.
4. For management, also grant **Create Issues**, **Edit Issues**, **Schedule
   Issues**, **Assign Issues**, **Assignable User**, **Transition Issues**, and
   **Add Comments**. **Schedule Issues** is required to add or remove issues
   from a sprint.
5. Grant the Jira global **Browse users and groups** permission if `!jira assign`
   should resolve display names or email addresses.
6. Do not grant project administration or issue deletion just for this bot. It
   has no delete command.

For a company-managed project, permissions normally come from its permission
scheme and project roles. For a team-managed project, use **Project settings >
Access**. Jira's labels vary by plan, so ask a Jira site administrator to map
the permissions above if you cannot see those controls.

### Create the API Token

This bot uses the ordinary API-token basic-auth flow intended for private
scripts and bots. It does not use your Atlassian password.

1. Sign in as the dedicated account at the [Atlassian API token page](https://id.atlassian.com/manage-profile/security/api-tokens).
2. Select **Create API token**. Do not select **Create API token with scopes**;
   scoped tokens use a different API gateway URL that this configuration does
   not use.
3. Name it `jas-ai-railway`.
4. Choose an expiration date and record it so the token can be rotated before
   it expires.
5. Create the token, copy it once, and store it in a password manager.

### Add the Railway Variables

Open the **JAS-AI bot service**, select **Variables**, and add:

```text
JIRA_BASE_URL=https://your-site.atlassian.net
JIRA_EMAIL=jas-ai-account@example.com
JIRA_API_TOKEN=YOUR_ATLASSIAN_API_TOKEN
JIRA_PROJECT_KEYS=JAS,OPS
JIRA_BOARD_ID=1
JIRA_DEFAULT_ISSUE_TYPE=Story
JIRA_SYNC_INTERVAL_SECONDS=600
```

Use the project keys shown at the start of issue IDs, not project display names.
For example, `JAS-42` belongs to project key `JAS`. Do not add `/jira`, `/browse`,
or `/rest` to `JIRA_BASE_URL`, and do not quote the values.

`JIRA_BOARD_ID` is the number after `/boards/` in the Jira board URL. For
`.../projects/SCRUM/boards/1`, set it to `1`. The bot resolves the active sprint
from this board when you request the "current sprint" and uses the board's
configured estimation field when setting story points.

In Jira, open the board's settings and confirm its estimation method is **Story
points**. Also ensure the account in `JIRA_EMAIL` can view this board. If the
board's filter is private, share it with that account or a group/role containing
that account.

`JIRA_PROJECT_KEYS` is also a write boundary: the bot refuses to create or
modify an issue outside those projects, even if the Jira account has broader
access. Apply the Railway variable changes and redeploy after completing the
next section.

## 7. Configure and Deploy the Bot Service

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
and applies all database migrations automatically.

## 8. Watch the First Deployment

Open the bot service's deployment logs. A successful startup should include
messages similar to:

```text
Applying database migration 001_create_discord_messages.sql
Applying database migration 002_add_long_term_search.sql
Applying database migration 003_create_google_drive_index.sql
Applying database migration 004_create_project_update_state.sql
Applying database migration 005_create_jira_integration.sql
Logged in as Jarrett AI
Connected to 1 server(s)
Recording 2 configured channel(s)
Starting full backfill for Discord channel 123456789012345678
Finished full backfill for channel 123456789012345678; stored 850 message(s)
Starting Google Drive sync for folder 1AbCdEfGhIjKlMnOp
Google Drive sync complete: 1 root(s), 42 file(s) seen, 40 indexed, 0 unchanged, 2 skipped
Jira sync completed for JAS: 28 issue(s)
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

The Google Drive sync runs in the background at startup and then at the
configured interval. A skipped-file warning includes the file name and reason;
it does not stop the rest of the sync.

## 9. Test the Bot in Discord

In an approved channel, send:

```text
!ping
```

The bot should answer:

```text
Pong!
```

After the first Google Drive sync completes, ask about a distinctive phrase in
one of the files:

```text
@JAS AI What does the equipment checklist say about camera repairs?
```

When an answer relies on Drive content, the bot is instructed to name the
source file and include its Google Drive link when useful.

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

Test the read-only Jira index:

```text
!jira search camera repair
```

Test the write approval boundary with a real, low-risk issue. This first posts
a preview and does not send a write request to Jira:

```text
!jira comment JAS-42 | Testing Jarrett AI's approval flow.
```

You can also request a change naturally by mentioning the bot:

```text
@JAS AI Create a Story in SCRUM to document our Perforce workflow and include acceptance criteria.
```

For an explicit Jira write request, Jarrett AI extracts the proposed fields and
posts the same approval preview automatically. When only one project is listed
in `JIRA_PROJECT_KEYS`, the bot uses it automatically. If you do not name an
issue type, it uses `JIRA_DEFAULT_ISSUE_TYPE` (`Story` by default). Priority and
assignee are not required for creation. You do not need to type an approval
phrase.

Follow-up mentions use the recent channel conversation, so messages such as
`what options do you have for those?` or `make it a User Story` can refer to the
preceding exchange. This works only in channels included in
`LISTEN_CHANNEL_IDS`. In any other server channel, the bot explains that memory
and connected tools are unavailable instead of answering without context.

Click **Cancel** to verify that nothing changes. Run it again and click
**Confirm** to apply it. Only the Discord user who issued the command can use
that request's buttons, and an unconfirmed request expires after 15 minutes.

## 10. Verify the PostgreSQL Data

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

Check the Google Drive index and any skipped files:

```sql
SELECT name, mime_type, indexed_at, last_error
FROM google_drive_files
ORDER BY name;
```

Check the number of searchable chunks:

```sql
SELECT COUNT(*) AS searchable_drive_chunks
FROM google_drive_chunks;
```

Check the scheduled project report state:

```sql
SELECT
    channel_id,
    last_started_at,
    last_completed_at,
    last_message_id,
    last_error
FROM project_update_state;
```

Check synchronized Jira issues and their last update:

```sql
SELECT issue_key, summary, status, assignee, updated_at, synced_at
FROM jira_issues
ORDER BY updated_at DESC
LIMIT 20;
```

Check Jira project sync state and the write-approval audit trail:

```sql
SELECT project_key, last_synced_at FROM jira_sync_state ORDER BY project_key;

SELECT action_type, status, created_at, completed_at, result_text, error
FROM jira_pending_actions
ORDER BY created_at DESC
LIMIT 20;
```

Exit `psql` with:

```text
\q
```

## 11. Run Locally (Optional)

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
$env:GOOGLE_DRIVE_FOLDER_IDS="YOUR_GOOGLE_DRIVE_FOLDER_ID"
$env:GOOGLE_SERVICE_ACCOUNT_JSON_BASE64=[Convert]::ToBase64String(
    [IO.File]::ReadAllBytes("$HOME\Downloads\jas-ai-drive-reader.json")
)
$env:PROJECT_UPDATE_CHANNEL_ID="YOUR_UPDATE_CHANNEL_ID"
$env:PROJECT_UPDATE_INTERVAL_HOURS="12"
$env:PROJECT_NAME="YOUR_PROJECT_NAME"
$env:JIRA_BASE_URL="https://your-site.atlassian.net"
$env:JIRA_EMAIL="jas-ai-account@example.com"
$env:JIRA_API_TOKEN="YOUR_ATLASSIAN_API_TOKEN"
$env:JIRA_PROJECT_KEYS="JAS,OPS"
$env:JIRA_BOARD_ID="1"
$env:JIRA_DEFAULT_ISSUE_TYPE="Story"

python bot.py
```

PowerShell `$env:` values apply only to the current terminal session. Opening a
new terminal requires setting them again.

## 12. Add Another Channel Later

1. Give the bot **View Channel**, **Read Message History**, and **Send
   Messages** permissions in the new channel.
2. Copy the new channel ID.
3. Append it to the Railway bot service's `LISTEN_CHANNEL_IDS` variable.
4. Redeploy the bot service.
5. Watch the logs for the new channel's `Finished full backfill` message.

Existing channel sync state is preserved. Only the newly added channel needs a
full import.

## 13. Add Scheduled Project Intelligence Updates

Jarrett AI can post a proactive report to a dedicated Discord channel every 12
hours. Each report reviews stored Discord activity, possible older follow-ups,
recent Google Drive material, recent Jira activity, and current web research.
It includes:

- Status, open questions, blockers, and possible follow-ups
- Prioritized next steps
- Growth and advertising ideas
- Current research with source links
- One wildcard project upgrade

### Create the Update Channel

1. Create a Discord text channel such as `jas-ai-updates`.
2. Give the bot **View Channel** and **Send Messages** permissions there.
3. With Discord Developer Mode enabled, right-click the new channel.
4. Select **Copy Channel ID**.
5. Open the Railway **JAS-AI** service and its **Variables** tab.
6. Add `PROJECT_UPDATE_CHANNEL_ID` with the copied numeric ID.
7. Apply the staged change and redeploy.

The first report runs approximately five minutes after deployment. Later
reports use the completion time stored in PostgreSQL, so a Railway restart does
not reset the 12-hour schedule or produce a duplicate report.

To adjust the schedule, set `PROJECT_UPDATE_INTERVAL_HOURS`. The minimum is one
hour. For example:

```text
PROJECT_UPDATE_INTERVAL_HOURS=12
```

These optional variables improve report targeting:

```text
PROJECT_NAME=Your Project Name
PROJECT_WEB_SEARCH_TOPICS=local competitors, industry trends, advertising channels
```

`PROJECT_WEB_SEARCH_TOPICS` is guidance, not a fixed query. The bot also derives
research topics from current project activity. The OpenAI web-search tool is
required during every report, so this feature adds two scheduled OpenAI calls
per day at the default interval plus web-search tool usage.

The Discord application owner can trigger a test immediately from Discord:

```text
!projectupdate
```

The result is posted in `PROJECT_UPDATE_CHANNEL_ID`. If you also want discussion
inside the update channel recorded as long-term memory, add its ID to
`LISTEN_CHANNEL_IDS`; that is optional for scheduled posting.

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
5. PostgreSQL searches indexed Google Drive chunks for the same topic.
6. PostgreSQL searches synchronized Jira summaries, descriptions, comments,
   and change history for the same topic.
7. Relevant old messages, recent conversation, Drive excerpts, Jira activity,
   and the current question are sent to OpenAI.
8. The answer is posted to Discord and stored like other Jarrett AI replies.

Jira sync runs once when the bot starts and every 10 minutes by default. The
first run imports every issue visible to the Jira account in the configured
projects, including comments and change history. Later runs use Jira's update
timestamp with a five-minute overlap so edits are not missed. This is polling,
so ordinary background ingestion can take up to the configured interval.
Jira-focused questions and scheduled project reports now request an incremental
refresh before reading the local index. Issue keys found in the question or
recent Discord conversation are refreshed directly, including their comments
and change history. If Jira is temporarily unavailable, the bot falls back to
the last successfully cached data. Before a scheduled report, the bot also
directly refreshes the 20 most recently tracked issues to avoid Jira search-index
delay hiding a newly posted comment.

The `!jira sync` result is an incremental count. For example, `0 changed
issue(s) imported` means Jira returned no changes since the previous sync; it
does not mean PostgreSQL contains zero Jira issues.

Scheduled project reports use a separate PostgreSQL state row to claim each
run, prevent overlapping reports, and remember the last successful completion.
They inspect new stored messages plus older keyword-matched follow-up
candidates. The model is instructed to label uncertain follow-up inferences
rather than presenting them as confirmed unfinished work.

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
- `!syncdrive` starts an immediate Drive refresh for the Discord application
  owner
- `!projectupdate` immediately generates a project report for the Discord
  application owner
- `!jira search <words or ISSUE-123>` searches the local Jira index; read-only
- `!jira create PROJECT | TYPE | SUMMARY | DESCRIPTION` proposes a new issue
- `!jira edit ISSUE-123 | FIELD | VALUE` proposes editing `summary`,
  `description`, `priority`, or comma-separated `labels`
- `!jira comment ISSUE-123 | TEXT` proposes a comment
- `!jira transition ISSUE-123 | STATUS` proposes a workflow transition
- `!jira assign ISSUE-123 | NAME OR EMAIL` proposes assignment; use
  `unassigned` to clear it
- `!jira plan ISSUE-123 | current/- | POINTS/- | PRIORITY/-` proposes sprint,
  estimation, and priority changes in one approval
- `!jira sync` immediately refreshes Jira for the Discord application owner

Every Jira create, edit, comment, transition, assignment, or planning request
creates a Discord preview with **Confirm** and **Cancel** buttons, whether it
came from a `!jira` command or an explicit natural-language request to
`@JAS AI`. No Jira write is made until the requesting user clicks **Confirm**.
Scheduled reports and ordinary questions can read Jira context but cannot
bypass this path.

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

### Google Drive Sync Finds Zero Files

Check all of the following:

- The Google Drive API is enabled in the same Google Cloud project as the
  service account
- The folder was shared directly with the service-account email as a Viewer
- `GOOGLE_DRIVE_FOLDER_IDS` contains the folder ID or URL, not its display name
- Both Google Drive variables are on the Railway bot service and were deployed
- For a Shared Drive, the service account was added as a drive member

### Google Drive Authentication Fails

Create a new JSON key for the service account, base64-encode the complete file,
replace `GOOGLE_SERVICE_ACCOUNT_JSON_BASE64`, and redeploy. Do not add quotes or
the variable name to its value. Delete the old key in Google Cloud if it may
have been exposed.

### Scheduled Project Updates Do Not Appear

Check that:

- `PROJECT_UPDATE_CHANNEL_ID` is a numeric channel ID, not the channel name
- The bot has **View Channel** and **Send Messages** in that channel
- `PROJECT_UPDATE_INTERVAL_HOURS` is a number of at least `1`
- The Railway variables were deployed, not merely staged
- The deployment logs do not show an OpenAI web-search or Discord permission
  error

Run `!projectupdate` as the Discord application owner to test immediately.

### Jira Sync Returns `401 Unauthorized`

Confirm `JIRA_EMAIL` is the email of the account that created the token,
`JIRA_API_TOKEN` is an ordinary API token rather than a password or scoped API
token, and `JIRA_BASE_URL` is the site's `https://name.atlassian.net` URL. Check
the token's expiration date, replace it if expired, then redeploy.

### Jira Sync Returns `403 Forbidden`

The authenticated Jira account lacks access. Grant **Browse Projects** in every
configured project. For write failures, grant the specific permission named by
the command, such as **Add Comments** or **Transition Issues**. Jira API access
never exceeds that account's normal Jira permissions.

### Jira Search Finds Nothing

Run `!jira sync` as the Discord application owner and inspect the deployment
logs. Confirm `JIRA_PROJECT_KEYS` uses issue-key prefixes, the account can browse
those projects, and migration `005_create_jira_integration.sql` was applied.

### A New Jira Comment Does Not Appear

Ask a Jira-focused question again or run `!jira sync` as the Discord application
owner. Jira-focused answers and project reports refresh Jira before reading the
local index, and references such as `SCRUM-6` trigger a direct issue refresh.
If the comment is restricted to a Jira role or group, the account configured in
`JIRA_EMAIL` must belong to that role or group. A zero-change sync is normal and
does not mean the existing index is empty.

### Mentions Say They Cannot See Jira but `!jira search` Works

Redeploy the latest code. Questions containing Jira, ticket, sprint, backlog,
or board language automatically include a compact overview of the 40 most
recently updated Jira issues. This lets questions such as `what tickets are
currently open?` work even when the words in the question do not appear in an
issue's summary or description.

### The Bot Forgets the Immediately Previous Message

Confirm the current channel ID appears in the bot service's
`LISTEN_CHANNEL_IDS`, then redeploy after changing the variable. The bot only
loads and stores conversation context in those approved channels. Current code
also stores its own replies immediately, so the next mention can resolve short
follow-ups such as `those`, `that ticket`, or `make it a Story`.

### A Jira Approval Button Says It Expired

Approval requests last 15 minutes and in-memory Discord buttons do not survive
a bot restart. Run the command again and review the new preview. An expired,
cancelled, or restart-orphaned request never makes a Jira write.

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

- Never commit or share `DISCORD_TOKEN`, `OPENAI_API_KEY`, `DATABASE_URL`,
  `DATABASE_PUBLIC_URL`, `JIRA_API_TOKEN`, or service-account credentials.
- Treat the Google service-account JSON as a password. Never commit it, post
  it in Discord, or include it in screenshots.
- Reset any secret immediately if it is exposed.
- Only record channels whose members know their messages are being retained.
- Review your retention policy before storing private or sensitive discussion.
- Keep PostgreSQL private unless public access is temporarily needed for local
  tools.
- Scheduled reports and answers may send selected Discord, Drive, and Jira
  excerpts to OpenAI. Reports also use live web search. Do not index material
  that report readers are not allowed to access.
- Use a dedicated Jira account, limit it to the configured projects, rotate its
  API token before expiration, and revoke the token immediately if exposed.

## Official Documentation

- [Discord privileged intents](https://discord.com/developers/docs/events/gateway#privileged-intents)
- [OpenAI API quickstart](https://developers.openai.com/api/docs/quickstart/)
- [OpenAI Responses web search](https://developers.openai.com/api/docs/guides/tools-web-search)
- [OpenAI function calling](https://developers.openai.com/api/docs/guides/function-calling)
- [Google service-account authentication](https://developers.google.com/identity/protocols/oauth2/service-account)
- [Google Drive file search](https://developers.google.com/workspace/drive/api/guides/search-files)
- [Google Drive downloads and exports](https://developers.google.com/workspace/drive/api/guides/manage-downloads)
- [Jira Cloud REST API v3](https://developer.atlassian.com/cloud/jira/platform/rest/v3/intro/)
- [Jira API-token basic authentication](https://developer.atlassian.com/cloud/jira/service-desk/basic-auth-for-rest-apis/)
- [Atlassian API-token management](https://support.atlassian.com/atlassian-account/docs/manage-api-tokens-for-your-atlassian-account/)
- [Jira issue API](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/)
- [Jira user search API](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-user-search/)
- [Jira sprint API](https://developer.atlassian.com/cloud/jira/software/rest/api-group-sprint/)
- [Jira board API](https://developer.atlassian.com/cloud/jira/software/rest/api-group-board/)
- [Jira estimation API](https://developer.atlassian.com/cloud/jira/software/rest/api-group-issue/)
- [Jira work item permissions](https://support.atlassian.com/jira-cloud-administration/docs/work-item-permissions/)
- [Railway PostgreSQL](https://docs.railway.com/databases/postgresql)
- [Railway reference variables](https://docs.railway.com/variables#referencing-another-services-variable)
- [Railway start commands](https://docs.railway.com/deployments/start-command)
- [Railway CLI database connection](https://docs.railway.com/cli/connect)
- [PostgreSQL full-text search](https://www.postgresql.org/docs/current/textsearch-tables.html)
