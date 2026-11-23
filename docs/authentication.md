# Authentication

Every mode talks to Google with a Google OAuth access token that carries a Tasks scope. What
differs is who obtains that token: you, once, ahead of time (`token`); a gateway in front of the
server (`resource-server`); or the server itself (`oauth-proxy`). This page covers the Google
Cloud setup all three share, the scopes, how to get a refresh token for `token` mode, and the
keys the multi-user modes need. Variable details are in [configuration.md](configuration.md).

## Google Cloud setup

You need your own OAuth client. The Tasks API needs no billing and, for the scopes used here,
no security assessment.

1. In the [Google Cloud console](https://console.cloud.google.com), create a project (or pick
   one).

2. **APIs & Services → Library:** enable the **Google Tasks API**.

3. **Google Auth Platform → Branding / Audience** (the OAuth consent screen):
   - User type **External** (Internal is available only inside a Google Workspace
     organization, and is fine if every user belongs to it).
   - Fill in the app name and support email.
   - **Data access:** add the scopes from [Scopes](#scopes) for the modes you will run.
   - **Publishing status:** while the app is in **Testing**, only listed test users can sign
     in and their refresh tokens expire after 7 days. For anything long-lived, set it to
     **In production**. Without verification, users see a one-time "Google hasn't verified this
     app" screen (Advanced → Continue); that is expected for a self-hosted app.

4. **Google Auth Platform → Clients → Create client:** application type **Web application**.

5. Under **Authorized redirect URIs**, add every callback the deployment uses:

   | Used by | Redirect URI |
   |---|---|
   | `oauth-proxy` mode | `{MCP_BASE_URL}/auth/callback`, for example `https://tasks.example.com/auth/callback` (the path follows `GOOGLE_OAUTH_REDIRECT_PATH`) |
   | Obot Static OAuth (`resource-server` behind Obot) | `https://<obot-host>/oauth/mcp/callback` |
   | Getting a refresh token with the OAuth Playground | `https://developers.google.com/oauthplayground` (remove it afterwards) |

   For local `oauth-proxy` testing, `http://localhost:8000/auth/callback` works; Google allows
   plain `http` only for `localhost`.

6. Save, and copy the **Client ID** and **Client secret**. They become
   `GOOGLE_OAUTH_CLIENT_ID` and `GOOGLE_OAUTH_CLIENT_SECRET`.

One client can serve all modes at once; the redirect URIs simply accumulate.

## Scopes

| Scope | Requested or required when |
|---|---|
| `https://www.googleapis.com/auth/tasks` | Default: read and write tasks |
| `https://www.googleapis.com/auth/tasks.readonly` | Instead of the above when `GOOGLE_TASKS_READONLY=true` |
| `openid` | `oauth-proxy` mode, to identify the user; recommended in `token` mode |
| `https://www.googleapis.com/auth/userinfo.email` | `oauth-proxy` mode; recommended in `token` mode |

Per mode:

- **`oauth-proxy`** requests `openid`, `userinfo.email` and the Tasks scope, and rejects
  tokens without all three.
- **`resource-server`** requires only the Tasks scope on the forwarded token. In read-only mode
  a token carrying either `tasks` or `tasks.readonly` is enough. Configure the gateway to request
  the same scopes (on Obot, the Static OAuth scope list).
- **`token`** uses whatever scopes the refresh token was granted. The tools need the Tasks scope;
  `get_current_user` calls Google's userinfo endpoint, which needs `openid` and
  `userinfo.email` to return the account's ID and email.

The scope only limits what Google accepts. Read-only mode also removes the write tools from the
server, so a model cannot even attempt a change. See [tools.md](tools.md).

## Single-user refresh token

`token` mode needs `GOOGLE_OAUTH_REFRESH_TOKEN` plus the client ID and secret it was issued to.
The server exchanges the refresh token for access tokens as needed. A refresh token is
equivalent to a password for your task data: keep it out of shell history, tracked files and
chat transcripts, and store it in a secret manager, an untracked `.env` file, or a field your
host marks as sensitive (Obot does this for catalog fields marked `sensitive`).

### With the OAuth Playground and your own client

This keeps the token issued to *your* client, so you can revoke it independently of anything
else.

1. Add `https://developers.google.com/oauthplayground` as an authorized redirect URI on your
   Web client (step 5 above).

2. Open the [OAuth 2.0 Playground](https://developers.google.com/oauthplayground). Click the
   gear icon, tick **Use your own OAuth credentials**, and paste your client ID and secret.
   Leave **Access type** as **Offline**.

3. In **Step 1**, type these scopes into the input box (space separated) and click
   **Authorize APIs**:

   ```text
   https://www.googleapis.com/auth/tasks openid https://www.googleapis.com/auth/userinfo.email
   ```

   Use `tasks.readonly` instead of `tasks` if the server will only ever read.

4. Sign in with the Google account whose tasks the server should manage, and approve.

5. In **Step 2**, click **Exchange authorization code for tokens**. Copy the **Refresh token**
   into `GOOGLE_OAUTH_REFRESH_TOKEN`.

6. Remove the Playground redirect URI from your client again, and close the Playground tab.

Google revokes a refresh token when the user revokes access (at
<https://myaccount.google.com/permissions>), when it goes unused for six months, after 7 days
while the app is in Testing, and when an account accumulates too many refresh tokens for the
same client. When that happens tool calls
fail with an authentication error; get a new token the same way.

### Access token only

`GOOGLE_OAUTH_ACCESS_TOKEN` alone also works, for a quick test: access tokens expire after about
an hour and, without a refresh token, the server cannot renew them.

### How the server uses the tokens

- If `GOOGLE_OAUTH_ACCESS_TOKEN` is set, it is used first, for as long as Google accepts it.
- When Google answers `401`, the server refreshes (if `GOOGLE_OAUTH_REFRESH_TOKEN`, client ID and
  secret are set) and retries that request once.
- A refreshed token is reused until 60 seconds before its `expires_in`, then refreshed
  proactively. Concurrent calls share one refresh.
- With only a refresh token, the first call refreshes.

## Multi-user keys

### `JWT_SIGNING_KEY` (`oauth-proxy`)

In `oauth-proxy` mode the server issues its own tokens to MCP clients and signs them with a key
derived from `JWT_SIGNING_KEY`. Any long random string works; generate one with:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

If it is unset, FastMCP derives a key from the client secret. Set it explicitly whenever you use
persistent storage (startup fails otherwise), so issued tokens do not depend on the client secret
and stay valid across restarts.

### `STORAGE_ENCRYPTION_KEY` (`oauth-proxy` with persistent storage)

With `TOKEN_STORAGE_BACKEND=filetree` or `redis`, client registrations and each user's upstream
Google tokens are stored encrypted with Fernet. The key must be a 32-byte url-safe base64 Fernet
key; the server validates it at startup. Generate one with:

```bash
uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

### `MCP_AUTH_TOKEN` (`token` over HTTP)

A shared bearer token clients must send as `Authorization: Bearer <value>`. It protects a
single-user HTTP server, which would otherwise act on your tasks for anyone who can reach it.
Generate it like `JWT_SIGNING_KEY`. The comparison is constant-time.

## Key rotation

| Rotate | Effect | What users see |
|---|---|---|
| `STORAGE_ENCRYPTION_KEY` | Stored entries no longer decrypt; they are treated as missing, not as errors | Every user and MCP client signs in again; no `500`s |
| `JWT_SIGNING_KEY` | Every token the server issued so far fails verification | Every MCP client re-authenticates |
| Google client secret | `oauth-proxy` can no longer exchange or refresh codes with the old secret; if `JWT_SIGNING_KEY` is unset, issued tokens are invalidated too | Users sign in again |
| `MCP_AUTH_TOKEN` | Old value is rejected immediately | Update the client header |
| `GOOGLE_OAUTH_REFRESH_TOKEN` | Revoke the old one in the Google account's permissions page | None, once the new one is deployed |

Rotation takes effect on restart. There is no dual-key window, so rotate during a quiet period.
In `resource-server` mode the server holds no keys of its own; rotate the client secret in
Google Cloud and in Obot's Static OAuth settings together.
