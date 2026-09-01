"""Who is asking, what they are entitled to, and a record that they asked.

THE APPLICATION AUTHENTICATES NOBODY
------------------------------------
There is no password field, no session store and no token in this codebase, and
there should never be one. Goldenrod reads the identity its platform has already
proved: Google Cloud IAP puts it in `X-Goog-Authenticated-User-Email` on every
request that reaches the service, and a request that did not come through IAP
never reaches the service at all. What this module does is the second half —
look that subject up in `access_grants` and turn it into a role.

That split is the reason the deploy needs no code change: the header this reads
is the header IAP writes.

    X-Goog-Authenticated-User-Email: accounts.google.com:props@fayoum

Locally there is no IAP, so `X-Goldenrod-Subject` is accepted as well and the
identity chooser in the interface stands in for the platform's account picker.
Both are switched off by setting GOLDENROD_IDENTITY_CHOOSER=0, which is what a
deployment behind IAP does.

FAIL CLOSED, AND SAY WHY
------------------------
No header, an unknown subject, an empty roster, a role that no longer exists:
every one of them is a refusal, and every refusal is written to `access_log`
before it is returned. A denial nobody can see afterwards is not governance.

Every read is logged, not only the denials. The question a production office
actually asks is "who opened the pages last night", and that has no answer
unless the successful reads are recorded too.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import unquote

from services.common.access import Role, get_role
from services.common.mcp_client import ClickHouseMCP, MCPError
from services.common.sql import insert, lit

# What Cloud IAP sets. The value is prefixed with the identity namespace —
# `accounts.google.com:someone@example.com` — which is stripped below.
IAP_HEADER = "x-goog-authenticated-user-email"
# Local development, and the interface's identity chooser. Ignored entirely when
# the chooser is switched off.
DEV_HEADER = "x-goldenrod-subject"
# The same thing as a cookie, because EventSource cannot send a header and the
# streamed check is an EventSource. Same-origin, and read only when the chooser
# is on — behind IAP this is dead code that never runs.
DEV_COOKIE = "goldenrod_subject"

AUDIT_COLUMNS = [
    "event_id", "production_id", "subject", "role", "action", "granted",
    "released", "withheld", "detail", "at",
]


class AccessDenied(Exception):
    """The request carried no usable identity, or one with no grant.

    Carries the HTTP status so the API layer does not have to guess: 401 when
    nobody is identified, 403 when somebody is and is not entitled.
    """

    def __init__(self, detail: str, status: int = 403):
        super().__init__(detail)
        self.detail = detail
        self.status = status


@dataclass(frozen=True)
class Viewer:
    """One identified person and the role they hold on this production."""

    subject: str
    display_name: str
    role: Role
    production_id: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "display_name": self.display_name or self.subject,
            "production_id": self.production_id,
            "role": self.role.as_dict(),
        }


def identity_chooser_enabled() -> bool:
    """Whether the interface may pick an identity for itself.

    On behind a login-less local run; off behind IAP, where the platform has
    already decided who this is and offering a chooser would be theatre.
    """
    return os.environ.get("GOLDENROD_IDENTITY_CHOOSER", "1").strip() not in ("0", "false", "no")


def subject_from_headers(headers: Mapping[str, str]) -> str:
    """The identity the platform proved, or '' if it proved none.

    IAP's header wins over the development one whenever both are present: if the
    request really did come through IAP, nothing a client sends may override it.
    """
    raw = headers.get(IAP_HEADER, "") or headers.get(IAP_HEADER.title(), "")
    if raw:
        # `accounts.google.com:someone@example.com` -> `someone@example.com`
        return raw.rsplit(":", 1)[-1].strip().lower()
    if not identity_chooser_enabled():
        return ""
    header = str(headers.get(DEV_HEADER, "")).strip().lower()
    return header or _cookie(str(headers.get("cookie", "")), DEV_COOKIE)


def _cookie(header: str, name: str) -> str:
    """One cookie value, percent-decoded.

    The interface writes it with encodeURIComponent, which escapes the `@` in
    every email address it will ever hold.
    """
    for part in header.split(";"):
        key, _, value = part.partition("=")
        if key.strip() == name:
            return unquote(value.strip().strip('"')).lower()
    return ""


@dataclass
class Directory:
    """The access roster and the audit trail, through the MCP server.

    Not cached. A roster read is one small query, and paying for it per request
    is what makes revoking somebody take effect on their next click rather than
    on the next restart.
    """

    ch: ClickHouseMCP
    production_id: str

    # -- roster --------------------------------------------------------------
    def roster(self) -> list[dict[str, Any]]:
        """Everyone with a grant on this production, newest grant per subject."""
        return self.ch.rows(
            "SELECT subject, display_name, role, granted_by, granted_at "
            "FROM access_grants FINAL "
            f"WHERE production_id = {lit(self.production_id)} "
            "ORDER BY role, subject"
        )

    def identities(self) -> list[dict[str, Any]]:
        """The roster as the identity chooser shows it: who, and as what."""
        out = []
        for row in self.roster():
            role = get_role(str(row["role"]))
            if role is None:
                continue
            out.append(
                {
                    "subject": str(row["subject"]),
                    "display_name": str(row["display_name"]) or str(row["subject"]),
                    "role": role.as_dict(),
                }
            )
        return out

    def viewer(self, subject: str) -> Viewer:
        """Resolve a subject to a viewer, or refuse and record the refusal."""
        subject = str(subject or "").strip().lower()
        if not subject:
            self.log(
                subject="(anonymous)", role="", action="denied", granted=False,
                detail="request carried no proved identity",
            )
            raise AccessDenied(
                "no identity on the request. Behind Cloud IAP this cannot happen; "
                "locally, send X-Goldenrod-Subject or use the identity chooser.",
                status=401,
            )

        rows = self.roster()
        if not rows:
            self.log(
                subject=subject, role="", action="denied", granted=False,
                detail=f"no access roster exists for {self.production_id}",
            )
            raise AccessDenied(
                f"no access roster for production {self.production_id!r}. Nobody is "
                f"entitled to anything until one is loaded: "
                f"python -m services.loader.seed --dir data/fixtures",
            )

        match = next((r for r in rows if str(r["subject"]) == subject), None)
        if match is None:
            self.log(
                subject=subject, role="", action="denied", granted=False,
                detail=f"no grant on {self.production_id}",
            )
            raise AccessDenied(
                f"{subject} holds no grant on production {self.production_id!r}."
            )

        role = get_role(str(match["role"]))
        if role is None:
            # A grant naming a role the policy no longer declares. Denying is the
            # only safe reading: the alternative is inventing permissions for a
            # role nobody has defined.
            self.log(
                subject=subject, role=str(match["role"]), action="denied",
                granted=False, detail="grant names a role that no longer exists",
            )
            raise AccessDenied(
                f"{subject} is granted the role {match['role']!r}, which is not "
                f"declared in services/common/access.py. Access refused."
            )

        return Viewer(
            subject=subject,
            display_name=str(match["display_name"]) or subject,
            role=role,
            production_id=self.production_id,
        )

    # -- audit ---------------------------------------------------------------
    def log(
        self,
        *,
        subject: str,
        role: str,
        action: str,
        granted: bool,
        released: int = 0,
        withheld: int = 0,
        detail: str = "",
    ) -> None:
        """Append one row to the audit trail.

        Deliberately swallows its own failure. An audit write that breaks the
        check would mean the safest possible configuration — logging everything
        — is also the most fragile, and the pressure would be to switch it off.
        The write is best-effort; the access decision above it is not.
        """
        row = {
            "event_id": str(uuid.uuid4()),
            "production_id": self.production_id,
            "subject": subject,
            "role": role,
            "action": action,
            "granted": 1 if granted else 0,
            "released": max(0, int(released)),
            "withheld": max(0, int(withheld)),
            "detail": detail[:500],
            "at": datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0),
        }
        try:
            self.ch.run_query(insert("access_log", AUDIT_COLUMNS, [row]))
        except MCPError:
            pass

    def trail(self, limit: int = 100) -> list[dict[str, Any]]:
        """The audit trail, newest first. Append-only, so never FINAL."""
        return self.ch.rows(
            "SELECT subject, role, action, granted, released, withheld, detail, at "
            f"FROM access_log WHERE production_id = {lit(self.production_id)} "
            f"ORDER BY at DESC LIMIT {int(limit)}"
        )
