"""ABOUTME: Microsoft Graph as the mailbox owner — device-code sign-in (delegated Mail.ReadWrite), no app secret.
ABOUTME: The token can only reach mailboxes its owner can open. Also the folder index and protected folders."""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

GRAPH = "https://graph.microsoft.com/v1.0"
SCOPE = "https://graph.microsoft.com/Mail.ReadWrite offline_access"
# Never a move destination, nor anything beneath them: moving there would delete, hide or send mail.
PROTECTED = ("deleteditems", "junkemail", "drafts", "sentitems", "outbox", "recoverableitemsdeletions",
             "conversationhistory", "syncissues")


class GraphError(RuntimeError):
    pass


class Auth:
    """Device-code sign-in and refresh-token storage (file mode 600)."""

    def __init__(self, tenant: str, client_id: str, token_path: str):
        self.tenant, self.client_id = tenant, client_id
        self.path = os.path.expanduser(token_path)

    def _post(self, endpoint: str, data: dict) -> dict:
        url = f"https://login.microsoftonline.com/{self.tenant}/oauth2/v2.0/{endpoint}"
        body = urllib.parse.urlencode({"client_id": self.client_id, **data}).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=body), timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read())
            except ValueError:
                return {"error": f"http_{e.code}"}

    def login(self, allowed_users: set[str], whoami) -> str:
        """Interactive sign-in. Saves the token only if the signed-in user is allowed. Returns the user."""
        dc = self._post("devicecode", {"scope": SCOPE})
        if "device_code" not in dc:
            raise GraphError(f"device code request failed: {dc.get('error_description') or dc.get('error')}")
        print(dc["message"], file=sys.stderr, flush=True)
        interval, deadline = int(dc.get("interval", 5)), time.time() + int(dc.get("expires_in", 900))
        while time.time() < deadline:
            time.sleep(interval)
            tok = self._post("token", {"grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                                       "device_code": dc["device_code"]})
            err = tok.get("error")
            if err == "authorization_pending":
                continue
            if err == "slow_down":
                interval += 5
                continue
            if err:
                raise GraphError(f"sign-in failed: {tok.get('error_description') or err}")
            user = whoami(tok["access_token"])
            if user not in allowed_users:
                raise GraphError(f"signed in as {user!r}, which is not in config.mailbox; nothing saved")
            self.save({"user": user, "refresh_token": tok["refresh_token"],
                       "saved": datetime.now(timezone.utc).isoformat(timespec="seconds")})
            return user
        raise GraphError("sign-in timed out; run login again")

    def load(self) -> dict:
        if not os.path.exists(self.path):
            raise GraphError("not signed in: run `zeroish auth login` as the mailbox owner")
        with open(self.path) as fh:
            return json.load(fh)

    def save(self, state: dict):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(state, fh)
        os.replace(tmp, self.path)

    def logout(self) -> bool:
        if os.path.exists(self.path):
            os.remove(self.path)
            return True
        return False

    def access_token(self) -> tuple[str, str, int]:
        state = self.load()
        tok = self._post("token", {"grant_type": "refresh_token", "scope": SCOPE,
                                   "refresh_token": state["refresh_token"]})
        if "access_token" not in tok:
            raise GraphError(f"sign-in expired or revoked ({tok.get('error')}); run `zeroish auth login` again")
        if tok.get("refresh_token"):
            state["refresh_token"] = tok["refresh_token"]
            self.save(state)
        return tok["access_token"], state["user"], int(tok.get("expires_in", 3600))


def whoami(access_token: str) -> str:
    req = urllib.request.Request(GRAPH + "/me?$select=mail,userPrincipalName",
                                 headers={"Authorization": f"Bearer {access_token}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        me = json.load(r)
    return (me.get("mail") or me.get("userPrincipalName") or "").lower()


class Mailbox:
    """Graph calls for one signed-in mailbox."""

    def __init__(self, auth: Auth):
        self.auth = auth
        self._token, self._expires, self.user = None, 0.0, auth.load()["user"]
        self._folders: dict | None = None

    def _bearer(self) -> str:
        if not self._token or time.time() > self._expires - 60:
            self._token, self.user, ttl = self.auth.access_token()
            self._expires = time.time() + ttl
        return self._token

    def request(self, method, path, params=None, body=None, headers=None):
        url = path if path.startswith("http") else GRAPH + path
        if params:
            url += "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(3):
            req = urllib.request.Request(url, data=data, method=method)
            req.add_header("Authorization", f"Bearer {self._bearer()}")
            if data is not None:
                req.add_header("Content-Type", "application/json")
            for k, v in (headers or {}).items():
                req.add_header(k, v)
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    payload = r.read()
                    return json.loads(payload) if payload else {}
            except urllib.error.HTTPError as e:
                if e.code in (429, 503, 504) and attempt < 2:
                    time.sleep(min(int(e.headers.get("Retry-After") or 5), 30))
                    continue
                raise GraphError(f"Graph {e.code} on {method} {path}: {e.read().decode(errors='replace')[:300]}") \
                    from None
        raise GraphError(f"Graph throttled on {method} {path}")

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    # ── folders ───────────────────────────────────────────────────────
    def folders(self) -> dict[str, dict]:
        """id -> {name, parent, path}, five levels deep."""
        if self._folders is not None:
            return self._folders
        index: dict[str, dict] = {}

        def walk(path, depth, parent_path):
            res = self.get(path, params={"$top": "200",
                                         "$select": "id,displayName,childFolderCount,parentFolderId"})
            for f in res.get("value", []):
                full = f"{parent_path}/{f['displayName']}" if parent_path else f["displayName"]
                index[f["id"]] = {"name": f["displayName"], "parent": f.get("parentFolderId"), "path": full}
                if depth < 5 and f.get("childFolderCount"):
                    walk(f"/me/mailFolders/{f['id']}/childFolders", depth + 1, full)

        walk("/me/mailFolders", 1, "")
        self._folders = index
        return index

    def protected_ids(self) -> set[str]:
        ids = set()
        for wk in PROTECTED:
            try:
                ids.add(self.get(f"/me/mailFolders/{wk}", params={"$select": "id"})["id"])
            except (GraphError, KeyError):
                pass
        return ids

    def resolve(self, wanted: str) -> str:
        """Folder id from an id, full path, unique path ending or unique name (case-insensitive)."""
        index = self.folders()
        if wanted in index:
            return wanted
        w = wanted.strip().strip("/").lower()
        exact = [fid for fid, f in index.items() if f["path"].lower() == w]
        if len(exact) == 1:
            return exact[0]
        matches = [fid for fid, f in index.items()
                   if f["name"].lower() == w or ("/" in w and f["path"].lower().endswith("/" + w))]
        if len(matches) == 1:
            return matches[0]
        if matches:
            raise GraphError(f"folder {wanted!r} is ambiguous: {sorted(index[f]['path'] for f in matches)}")
        raise GraphError(f"no folder {wanted!r} (zeroish never creates folders; make it in Outlook)")

    def is_protected(self, fid: str, protected: set[str]) -> bool:
        index, seen = self.folders(), set()
        while fid and fid in index and fid not in seen:
            if fid in protected:
                return True
            seen.add(fid)
            fid = index[fid]["parent"]
        return False

    # ── mail ──────────────────────────────────────────────────────────
    INBOX_FIELDS = ("id,subject,from,toRecipients,ccRecipients,receivedDateTime,bodyPreview,isRead,"
                    "conversationId,parentFolderId,webLink")

    def inbox(self, since_iso: str, limit: int) -> list[dict]:
        return self.get("/me/mailFolders/inbox/messages",
                        params={"$top": str(limit), "$select": self.INBOX_FIELDS + ",body",
                                "$filter": f"receivedDateTime ge {since_iso}", "$orderby": "receivedDateTime desc"},
                        headers={"Prefer": 'outlook.body-content-type="text"'}).get("value", [])

    def inbox_page(self, bookmark: str | None, limit: int, newest_first: bool) -> list[dict]:
        """One page of the backlog sweep. newest_first walks back from today (strictly older than the bookmark);
        otherwise forward from the oldest (strictly newer than the bookmark)."""
        params = {"$top": str(limit), "$select": self.INBOX_FIELDS + ",body",
                  "$orderby": f"receivedDateTime {'desc' if newest_first else 'asc'}"}
        if bookmark:
            params["$filter"] = f"receivedDateTime {'lt' if newest_first else 'gt'} {bookmark}"
        return self.get("/me/mailFolders/inbox/messages", params=params,
                        headers={"Prefer": 'outlook.body-content-type="text"'}).get("value", [])

    def inbox_count(self) -> int:
        return int(self.get("/me/mailFolders/inbox", params={"$select": "totalItemCount"}).get("totalItemCount", 0))

    def message(self, msg_id: str) -> dict:
        return self.get(f"/me/messages/{msg_id}", params={"$select": self.INBOX_FIELDS + ",body"},
                        headers={"Prefer": 'outlook.body-content-type="text"'})

    def sent_history(self, lookback: int) -> list[dict]:
        out, path, params = [], "/me/mailFolders/sentitems/messages", \
            {"$top": "200", "$select": "toRecipients,ccRecipients,conversationId,sentDateTime",
             "$orderby": "sentDateTime desc"}
        while path and len(out) < lookback:
            res = self.get(path, params=params)
            out += res.get("value", [])
            path, params = res.get("@odata.nextLink"), None
        return out[:lookback]

    def ids_before(self, cutoff_iso: str, n: int) -> list[str]:
        """Up to n inbox message ids received before the cutoff, oldest first."""
        res = self.get("/me/mailFolders/inbox/messages",
                       params={"$filter": f"receivedDateTime lt {cutoff_iso}", "$orderby": "receivedDateTime asc",
                               "$top": str(min(n, 1000)), "$select": "id"})
        return [m["id"] for m in res.get("value", [])][:n]

    def count_before(self, cutoff_iso: str) -> int:
        res = self.get("/me/mailFolders/inbox/messages",
                       params={"$filter": f"receivedDateTime lt {cutoff_iso}", "$count": "true", "$top": "1",
                               "$select": "id"}, headers={"ConsistencyLevel": "eventual"})
        return int(res.get("@odata.count", 0))

    def batch_move(self, ids: list[str], folder_id: str) -> tuple[list[str], dict[str, str]]:
        """Move up to 20 messages in one Graph $batch call, retrying throttled items. Returns (moved, failed)."""
        pending, moved, failed = list(ids), [], {}
        for attempt in range(4):
            if not pending:
                break
            reqs = [{"id": str(i), "method": "POST", "url": f"/me/messages/{mid}/move",
                     "body": {"destinationId": folder_id}, "headers": {"Content-Type": "application/json"}}
                    for i, mid in enumerate(pending)]
            res = self.request("POST", "/$batch", body={"requests": reqs})
            retry, wait = [], 0
            for r in res.get("responses", []):
                mid = pending[int(r["id"])]
                if 200 <= r.get("status", 0) < 300:
                    moved.append(mid)
                elif r.get("status") in (429, 503, 504):
                    retry.append(mid)
                    wait = max(wait, int((r.get("headers") or {}).get("Retry-After", 5)))
                else:
                    failed[mid] = f"{r.get('status')}: {str(r.get('body'))[:120]}"
            pending = retry
            if pending:
                time.sleep(min(wait or 5, 30))
        for mid in pending:
            failed[mid] = "throttled"
        return moved, failed

    def move(self, msg_id: str, folder_id: str) -> str:
        return self.request("POST", f"/me/messages/{msg_id}/move", body={"destinationId": folder_id}).get("id")
