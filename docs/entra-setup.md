# Entra app setup (one-off, browser only)

Zeroish signs in **as you** (delegated permission). It needs no client secret and no tenant-wide access.

1. **Entra admin center → App registrations → New registration**
   - Name: anything, e.g. `Inbox Zeroish`
   - Supported account types: *Single tenant* for yourself, or *Multitenant* if you'll share it across companies
     (other tenants then need publisher verification, see below)
   - No redirect URI
2. **API permissions → Add → Microsoft Graph → Delegated → `Mail.ReadWrite`**
   - Add `User.Read` if it isn't already listed.
   - If your tenant blocks user consent, an admin clicks *Grant admin consent*. Otherwise you consent at sign-in.
3. **Authentication → Allow public client flows = Yes → Save.** Device-code sign-in needs this.
4. Copy the **Application (client) ID** and **Directory (tenant) ID** into `config.auth`.
5. `zeroish auth login`: open the printed URL, enter the code, sign in, and accept "Read and write access to your
   mail".

Revoke any time with `zeroish auth logout`, at https://myaccount.microsoft.com → *Apps*, or by revoking sessions.

## Sharing it with other companies

For a multitenant app used outside your tenant, Microsoft requires a **verified publisher** before ordinary users
can consent. Verification means linking a free Microsoft Cloud Partner Program ID to the app. Stricter tenants will
still want their admin to approve it. Microsoft 365 App Compliance (publisher attestation, then certification) is
optional, but enterprise buyers ask for it.
