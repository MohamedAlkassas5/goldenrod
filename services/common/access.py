"""Role-scoped access: who may see which finding, and who may write to the ledger.

WHY THIS EXISTS AT ALL
----------------------
An unreleased script is the most access-controlled document on a production.
Sides and call sheets circulate to the whole crew; full pages do not, and they
are watermarked per person when they do. So a check that reads the script and
publishes what it found has to answer "to whom" before it answers "what" — and
one of the three builder roles the hackathon names is the studio head enforcing
IAM and governance across agent workflows (SPEC §5).

THE POLICY IS CODE; THE ROSTER IS DATA
--------------------------------------
Roles and their permissions are declared here, in one table anyone can read.
*Who holds which role* lives in ClickHouse (`access_grants`), seeded like the
rest of the production's state and read through the MCP server like everything
else. Policy in code means a permission change is a diff; roster in the database
means adding a person is not a deploy.

EVERYTHING IN THIS MODULE IS A PURE FUNCTION
--------------------------------------------
Same reason as services/gate/rules.py: a scoping decision has to be testable
without a web server and without a database, because it is the one part of the
system where a bug is a leak rather than a wrong answer. The API calls these; it
does not reimplement them.

FAIL CLOSED
-----------
An element type nobody mapped, a finding with no departments, a subject not on
the roster — all of them deny. The failure mode of this module is a department
head who has to ring the production office, never a leaked page.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

# --- departments -----------------------------------------------------------
# The department that owns each kind of element. These are the crew departments
# that would actually be called about a break involving one, which is the only
# useful definition: a finding about the hero letter is the property master's
# problem, a finding about the flat is the location manager's.
ENTITY_DEPARTMENT: dict[str, str] = {
    "character": "cast",
    "prop": "props",
    "costume": "costume",
    "vehicle": "transport",
    "location": "locations",
    "set": "art",
    "symbol": "script",
}

# Every department that can own a finding. `script` is the catch-all for a break
# about the story rather than about a physical element: it belongs to the
# production office, and only the unrestricted roles see it.
DEPARTMENTS: tuple[str, ...] = (
    "script", "cast", "props", "costume", "art", "locations", "transport",
)

REDACTED = "[redacted — full pages are access-controlled]"


def departments_for(entity_types: Iterable[str]) -> list[str]:
    """Departments owning a set of element types. Unmapped types are dropped.

    Dropping rather than defaulting is deliberate: an element type nobody has
    mapped must not silently become visible to whichever department happens to
    sort first. A finding left with no department is owned by `script`, and
    `script` is visible only to the unrestricted roles.
    """
    found = {ENTITY_DEPARTMENT[t] for t in entity_types if t in ENTITY_DEPARTMENT}
    return sorted(found) or ["script"]


# --- roles -----------------------------------------------------------------
@dataclass(frozen=True)
class Role:
    """One production-office role, and exactly what it may do.

    `unrestricted` is not shorthand for "every department in the list". It is
    the difference between a role defined by the departments it owns and one
    defined by responsibility for the whole day: a 1st AD handed a new
    department next week should not need a code change.
    """

    name: str
    title: str
    departments: frozenset[str]
    unrestricted: bool = False
    read_script: bool = False      # verbatim pages, not just the call sheet
    write_ledger: bool = False     # may accept a deviation in the office's name
    read_ledger: bool = False      # the whole decision history, not just their own

    def sees(self, departments: Iterable[str]) -> bool:
        return self.unrestricted or bool(self.departments & set(departments))

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "departments": ["*"] if self.unrestricted else sorted(self.departments),
            "read_script": self.read_script,
            "write_ledger": self.write_ledger,
            "read_ledger": self.read_ledger,
        }


def _role(name: str, title: str, departments: Iterable[str] = (), **kw: Any) -> Role:
    return Role(name=name, title=title, departments=frozenset(departments), **kw)


# The roles. Deliberately few: the people a call sheet is actually addressed to,
# plus the departments that carry the cost when a revision breaks something they
# have already built or sourced.
ROLES: dict[str, Role] = {
    role.name: role
    for role in (
        _role(
            "first_ad", "1st Assistant Director",
            unrestricted=True, read_script=True, write_ledger=True, read_ledger=True,
        ),
        _role(
            "script_coordinator", "Script Coordinator",
            unrestricted=True, read_script=True, write_ledger=True, read_ledger=True,
        ),
        # Sees the whole day and the whole ledger and cannot accept a deviation
        # in the office's name. Oversight and authority are not the same grant.
        _role(
            "producer", "Producer",
            unrestricted=True, read_script=True, read_ledger=True,
        ),
        _role("props", "Property Master", ("props",)),
        _role("art", "Art Director", ("art", "props")),
        _role("locations", "Location Manager", ("locations", "transport")),
        _role("costume", "Costume Supervisor", ("costume",)),
    )
}


def get_role(name: str) -> Role | None:
    """The role by name, or None. Unknown names deny; they never default."""
    return ROLES.get(str(name or "").strip().lower())


# --- scoping ---------------------------------------------------------------
def redact(finding: dict, role: Role) -> dict:
    """One finding as this role may see it.

    Only verbatim script lines are removed, and the citation around them is
    kept: the property master still learns that scene 22 line 98 is what breaks,
    and still cannot read the page. Dropping the whole evidence object instead
    would leave a finding citing nothing, which CLAUDE.md rule 1 then requires be
    dropped — quietly turning an access rule into a missed catch.

    The logged decision stays, and that is deliberate rather than an oversight.
    The rule the whole module follows is need-to-know at the point of action: a
    department is told why the thing it built has changed, because a finding
    without its reason is one a department head can only escalate. What they
    cannot do is browse the ledger — every decision on the film, including the
    ones about scenes they are not on — and `scope_graph` below enforces that.
    """
    if role.read_script:
        return finding
    scoped = dict(finding)
    scoped["evidence"] = [
        {**item, "quote": REDACTED} if item.get("type") == "scene_line" else item
        for item in finding.get("evidence", [])
    ]
    scoped["redacted"] = True
    return scoped


def scope_findings(
    findings: Iterable[dict], role: Role
) -> tuple[list[dict], dict[str, Any]]:
    """Split findings into what this role may see, and a note about the rest.

    The withheld half is reported as a count and nothing else. That something
    was withheld is not worth hiding — a department head who can see two things
    were held back knows to ring the office — but the scene, the claim and the
    citations are exactly what the role is not entitled to.
    """
    released: list[dict] = []
    withheld = 0
    for finding in findings:
        if role.sees(finding.get("departments") or ["script"]):
            released.append(redact(finding, role))
        else:
            withheld += 1
    return released, {
        "withheld": withheld,
        "script_redacted": not role.read_script,
        "departments": ["*"] if role.unrestricted else sorted(role.departments),
    }


def scope_step(step: dict, role: Role) -> dict:
    """One pipeline step as this role may see it.

    The SQL survives — it names ids and fact keys, never pages — but the rows it
    returned do not, because those rows carry statements and quoted lines
    straight out of the script. The count is kept so the step still shows that
    the query did real work rather than appearing to have returned nothing.
    """
    if role.read_script:
        return step
    return {**step, "sample": [], "sample_withheld": int(step.get("rows") or 0)}


def scope_run(run: dict, role: Role) -> dict:
    """A whole GateRun as this role may see it.

    Four surfaces leak script content and each is handled here rather than in
    the API, so a new endpoint cannot forget one: the findings themselves, the
    findings already silenced by a logged deviation, the sample rows on every
    pipeline step, and the dropped-finding notes — which quote the citation that
    failed to place.
    """
    findings, note = scope_findings(run.get("findings", []), role)
    dismissed, dismissed_note = scope_findings(run.get("dismissed", []), role)
    scoped = dict(run)
    scoped["findings"] = findings
    scoped["dismissed"] = dismissed
    scoped["steps"] = [scope_step(s, role) for s in run.get("steps", [])]
    scoped["access"] = {
        **note,
        "withheld": note["withheld"] + dismissed_note["withheld"],
        "role": role.as_dict(),
    }
    if not role.read_script:
        scoped["dropped"] = []
    return scoped


# What a browse response is made of, and whether reading it means reading the
# script. `scenes` and `entities` are on the call sheet already; the other three
# are the pages, in structured form.
SCRIPT_SURFACES = ("facts", "knowledge_state", "dependencies")
LEDGER_SURFACES = ("decisions",)


def scope_graph(graph: dict, role: Role) -> dict:
    """The browse view as this role may see it.

    A department head keeps what a call sheet would already have told them —
    the scenes, the elements, and what has been committed against them, which is
    their own department's spend — and loses the two things a call sheet never
    carries: the script in structured form, and the production office's reasons.
    """
    scoped = dict(graph)
    withheld: list[str] = []
    if not role.read_script:
        for key in SCRIPT_SURFACES:
            if key in scoped:
                scoped[key] = []
                withheld.append(key)
    if not role.read_ledger:
        for key in LEDGER_SURFACES:
            if key in scoped:
                scoped[key] = []
                withheld.append(key)
    scoped["access"] = {"withheld_surfaces": withheld, "role": role.as_dict()}
    return scoped
