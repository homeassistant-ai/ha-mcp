# Microsoft 365 Integration

This document describes how to configure the Microsoft 365 tools in ha-mcp,
enabling Claude and other AI assistants to read and manage your Outlook
calendar and Microsoft To Do tasks directly from Home Assistant.

## Available tools

| Tool | Description |
|------|-------------|
| `ms365_get_calendar_events` | List events in a date range |
| `ms365_create_calendar_event` | Create a new calendar event |
| `ms365_update_calendar_event` | Update subject, time, location etc. |
| `ms365_delete_calendar_event` | Delete an event by ID |
| `ms365_get_todo_tasks` | List To Do tasks (by list / status) |
| `ms365_add_todo_task` | Add a task with optional due date |
| `ms365_complete_todo_task` | Mark a task as completed |

All tools are auto-discovered by the existing `tools_*` registry — no changes
to `server.py` or `registry.py` are needed.

---

## Prerequisites — Azure App Registration

You need an Azure app with delegated Graph API permissions.

### Step 1 — Create the app

1. Go to [portal.azure.com](https://portal.azure.com) →
   **Azure Active Directory** → **App registrations** → **New registration**
2. Name: `ha-mcp-ms365`
3. Supported account types:
   **Personal Microsoft accounts** (Outlook.com / Hotmail)
   — or "Accounts in any organizational directory and personal accounts"
4. Redirect URI platform: **Mobile and desktop applications** → `http://localhost`
5. Click **Register** — note down **Application (client) ID**
6. **Certificates & secrets** → **New client secret** — note the value
7. **API permissions** → **Add** → **Microsoft Graph** → **Delegated**:
   - `Calendars.ReadWrite`
   - `Tasks.ReadWrite`
   - `offline_access`

### Step 2 — Get a refresh token (device code flow)

```bash
CLIENT_ID="your-application-client-id"
TENANT="common"   # use 'common' for personal Microsoft accounts

# Request device code
curl -s -X POST \
  "https://login.microsoftonline.com/$TENANT/oauth2/v2.0/devicecode" \
  -d "client_id=$CLIENT_ID&scope=Calendars.ReadWrite%20Tasks.ReadWrite%20offline_access"
```

Follow the instructions printed — go to **https://microsoft.com/devicelogin**
and enter the displayed code.

```bash
# Exchange device code for tokens
curl -s -X POST \
  "https://login.microsoftonline.com/$TENANT/oauth2/v2.0/token" \
  -d "grant_type=urn:ietf:params:oauth:grant-type:device_code&client_id=$CLIENT_ID&device_code=DEVICE_CODE_HERE"
```

Copy the `refresh_token` value from the response.

---

## Configuration

### Home Assistant Add-on (config tab)

```yaml
ms365:
  client_id: "your-azure-client-id"
  client_secret: "your-azure-client-secret"
  tenant_id: "common"
  users:
    - name: "Andreas"
      refresh_token: "0.AAAA..."
    - name: "Sonia"
      refresh_token: "0.BBBB..."
      calendar_id: "AQMkADAwATM0..."   # optional: shared/secondary calendar ID
```

### Environment variables (Docker / manual install)

```bash
MS365_CLIENT_ID=your-azure-client-id
MS365_CLIENT_SECRET=your-azure-client-secret
MS365_TENANT_ID=common

# Primary user
MS365_REFRESH_TOKEN=0.AAAA...

# Additional family members (NAME uppercased)
MS365_REFRESH_TOKEN_SONIA=0.BBBB...
MS365_CALENDAR_ID_SONIA=AQMkADAwATM0...   # optional secondary calendar
```

---

## Multi-user support

Pass the `user` parameter to target a specific family member's calendar:

```
"What's on Sonia's calendar this week?"
→ ms365_get_calendar_events(user="sonia")

"Add football practice to Sonia's calendar Thursday at 4pm"
→ ms365_create_calendar_event(
    subject="Football practice",
    start="2026-05-07T16:00:00",
    end="2026-05-07T17:30:00",
    user="sonia"
  )
```

Token resolution order per user:
1. `MS365_REFRESH_TOKEN_<NAME>` (e.g. `MS365_REFRESH_TOKEN_SONIA`)
2. Falls back to `MS365_REFRESH_TOKEN` (primary user)

---

## Token refresh

Tokens are cached in memory and refreshed automatically before expiry.
The updated `refresh_token` returned by Microsoft is stored back into the
running environment so the server stays authenticated across long sessions.
For persistence across restarts, store updated tokens in the add-on config.

---

## Example prompts

Once configured, Claude understands natural language requests such as:

- *"What do I have on my calendar this week?"*
- *"Add a dentist appointment Friday at 2pm in Helsingør"*
- *"Move my 3pm meeting to 4pm"*
- *"Cancel the appointment on Thursday"*
- *"What tasks do I have on my shopping list?"*
- *"Add 'buy flowers' to my To Do list with high priority"*
- *"Mark 'call the bank' as done"*
- *"Show me Sonia's calendar for next week"*
