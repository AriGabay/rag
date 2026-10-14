"""Which appraisals are one property: the link relation, the compatible closure and the per-turn property graph (round 12
U6: R1-R4, R8; AE1, AE2; KTD4, KTD5).

Round 11 keyed a unit by its block and parcel pairs, else by its street and house number, and two units with one key were
one property. An address carries no city in the stored identifiers, so the same street and number of two cities shared a
key. Round 12 replaces the key by a **relation between two units**, computed from their identities
(``identity.UnitIdentity``: parcels with sub-parcels, apartments, addresses and cities, read from the unit's own
identification parts) with the unit's stored title identifiers (``document_units.identifiers``) only as a fallback source
of the same facts for a kind of fact the reader found none of. The relation is a pure function (``relate``) and returns
``(relation, reason)``; the reasons are fixed strings, kept in the turn's record and the persisted answer:

| Pair evidence | Relation | Gathered |
|---|---|---|
| a shared block/parcel, sub-parcel and apartment absent on both sides or equal | ``parcel`` ("parcel B/P") | yes |
| the same street, number and city; parcels absent on a side; sub-parcel and apartment absent or equal | ``address_city`` | yes |
| the same parcel, or the same address and city, with different stated sub-parcels or apartments | ``siblings`` | no |
| the same parcel (or address and city), one side states a sub-parcel/apartment and the other none | ``candidate`` | no, counted in the coverage |
| the same street and number, a city missing on either side, no parcel link | ``candidate`` | no, counted in the coverage |
| stated cities differ, or both state disjoint parcel sets | ``other`` | no |
| anything else (nothing in common, or nothing to compare) | ``unrelated`` | no |

A stated city that differs is checked first (R1: two cities are never one property, whatever the parcel or the street).
Two cities are one place only by ``identity.same_place`` — never ``metadata.place_matches``, which takes any hyphen
extension for the same place — and a unit that states two different cities has none for linking (``UnitIdentity.link_city``).

**The graph** (``PropertyGraph``) is built once per turn over the visible, current subject units, memoized by the tools in
``ws.property_memo`` by data version, and sorted totally by (document id, unit number). It stores no links: they depend on
the caller's permissions and the visible set, so a hidden unit is not a node and nothing links *through* it.

**The focus property** (``PropertyGraph.gather``) is the compatible closure from the focus unit: breadth-first, a unit's
gathered neighbours in (relation strength, document id, unit number) order, and a unit joins only if it is compatible with
*every* member already gathered (no differing city, no disjoint stated parcels, no differing sub-parcel or apartment —
``Link.compatible``). So A{24, 40} gathers B{40}, but C{42} never joins through A when B is the seed. Each member keeps the
link it joined by (``Joined``: the member it came from and the link's reason); a non-member that is a ``candidate`` of a
member and conflicts with none is a named ``Candidate``; a non-member the graph cannot exclude (no identity facts, or
nothing in common to compare) is ``undetermined``. ``Gathered`` is what the inventory and the coverage record read.

**Everything else** (``PropertyGraph.partition``) is grouped without a seed: the units outside the focus set form a group
only when they are connected by gathered links and every pair of the component is compatible; otherwise each unit stays a
group of its own, so the grouping depends on neither the seed nor the document order. A unit without an identity is its
own group. The timeline reads a group id and a label from the partition."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from uuid import UUID

from sqlalchemy import Connection, text

from app.chat import contexts, identity
from app.chat import units as unit_store
from app.chat.dates import units_of
from app.chat.identity import UnitIdentity, same_place, unpad
from app.platform.source_kinds import APPRAISALS_SQL

# the relations (see the module doc) and the sets they form
PARCEL, ADDRESS_CITY, SIBLINGS, CANDIDATE, OTHER, UNRELATED = (
    "parcel", "address_city", "siblings", "candidate", "other", "unrelated")
GATHERED = frozenset({PARCEL, ADDRESS_CITY})
INCOMPATIBLE = frozenset({OTHER, SIBLINGS})
STRENGTH = {PARCEL: 0, ADDRESS_CITY: 1}  # the order a unit's gathered neighbours are visited in

# the reasons, as the coverage record and the persisted answer carry them
REASON_ADDRESS_CITY = "address_city"
REASON_PARCEL_PREFIX = "parcel "   # a parcel link's reason: this prefix and the shared parcels ("parcel 8401/40")
REASON_CITIES = "ערים שונות"
REASON_PARCELS = "קבוצות חלקות זרות"
REASON_SUB_PARCELS = "תתי-חלקות שונות"
REASON_APARTMENTS = "דירות שונות"
REASON_SUB_MISSING = "אותה חלקה, תת-חלקה לא צוינה"
REASON_ADDRESS_SUB_MISSING = "אותה כתובת, דירה או תת-חלקה לא צוינו"
REASON_CITY_MISSING = "עיר לא צוינה"
REASON_DIFFERENT_ADDRESS = "כתובת שונה"
REASON_NOTHING_TO_COMPARE = "אין נתון להשוואה"

LABEL_PARCELS = 3          # parcels a link reason or a label names before it stops
ABSENT, EQUAL, DIFFER, ONE_SIDED = "absent", "equal", "differ", "one_sided"

Key = tuple[str, int]      # (document id, unit number): the total order of the graph


@dataclass(frozen=True)
class Link:
    """The relation of two units and the fixed string that says why."""
    relation: str
    reason: str = ""

    @property
    def gathered(self) -> bool:
        return self.relation in GATHERED

    @property
    def compatible(self) -> bool:
        """No proof that the two are different properties (an ``other`` or a ``siblings`` link is such a proof)."""
        return self.relation not in INCOMPATIBLE


# --- the relation ----------------------------------------------------------------------------------------------------

def _parcels(i: UnitIdentity) -> dict[tuple[str, str], frozenset[str]]:
    """The unit's (block, parcel) pairs, each with the sub-parcels it states."""
    out: dict[tuple[str, str], set[str]] = {}
    for block, parcel, sub in i.parcels:
        out.setdefault((block, parcel), set())
        if sub:
            out[(block, parcel)].add(sub)
    return {k: frozenset(v) for k, v in out.items()}


def _stated(a: Iterable, b: Iterable) -> str:
    """Two sides' stated values of one attribute (a sub-parcel, an apartment): both absent, equal (a common value),
    different (none in common) or stated by one side only."""
    a, b = set(a), set(b)
    if not a and not b:
        return ABSENT
    if a and b:
        return EQUAL if a & b else DIFFER
    return ONE_SIDED


def _name(parcels: Iterable[tuple[str, str]]) -> str:
    shown = sorted(parcels, key=lambda k: (len(k[0]), k[0], len(k[1]), k[1]))
    more = "…" if len(shown) > LABEL_PARCELS else ""
    return ", ".join(f"{b}/{p}" for b, p in shown[:LABEL_PARCELS]) + more


def relate(a: UnitIdentity, b: UnitIdentity, pa: dict | None = None, pb: dict | None = None) -> Link:
    """The relation of two units' identities (KTD4's table, the module doc). Symmetric. ``pa`` and ``pb`` are the units'
    ``_parcels`` when the caller already has them (the graph keeps them per node)."""
    city_a, city_b = a.link_city, b.link_city
    if city_a and city_b and not same_place(city_a, city_b):
        return Link(OTHER, REASON_CITIES)
    pa = _parcels(a) if pa is None else pa
    pb = _parcels(b) if pb is None else pb
    shared = pa.keys() & pb.keys()
    if pa and pb and not shared:
        return Link(OTHER, REASON_PARCELS)
    apartments = _stated(a.apartments, b.apartments)
    if shared:
        subs = [_stated(pa[k], pb[k]) for k in shared]
        if DIFFER in subs:
            return Link(SIBLINGS, REASON_SUB_PARCELS)
        if apartments == DIFFER:
            return Link(SIBLINGS, REASON_APARTMENTS)
        if ONE_SIDED in subs or apartments == ONE_SIDED:
            return Link(CANDIDATE, REASON_SUB_MISSING)
        return Link(PARCEL, f"{REASON_PARCEL_PREFIX}{_name(shared)}")
    if a.addresses & b.addresses:
        subs = _stated({s for _, _, s in a.parcels if s}, {s for _, _, s in b.parcels if s})
        if DIFFER in (subs, apartments):
            return Link(SIBLINGS, REASON_SUB_PARCELS if subs == DIFFER else REASON_APARTMENTS)
        if not (city_a and city_b):
            return Link(CANDIDATE, REASON_CITY_MISSING)
        if ONE_SIDED in (subs, apartments):
            return Link(CANDIDATE, REASON_ADDRESS_SUB_MISSING)
        return Link(ADDRESS_CITY, REASON_ADDRESS_CITY)
    return Link(UNRELATED, REASON_DIFFERENT_ADDRESS if a.addresses and b.addresses else REASON_NOTHING_TO_COMPARE)


# --- the graph -------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Node:
    """One visible, current subject unit and what it says it is."""
    document_id: UUID | str
    version_id: UUID | str
    unit_no: int
    identity: UnitIdentity
    title: str | None = None

    @property
    def key(self) -> Key:
        return (str(self.document_id), self.unit_no)


@dataclass(frozen=True)
class Joined:
    """A unit gathered into the focus property: the member it was reached from and the link it joined by."""
    node: Node
    via: Key
    link: Link


@dataclass(frozen=True)
class Candidate:
    """An unlinked candidate of the focus property: a unit that may be the property but proves nothing (a city or a
    sub-parcel missing), with the member it was compared to and the reason."""
    node: Node
    link: Link
    against: Key


@dataclass(frozen=True)
class Gathered:
    """The focus property: its ``members`` in key order (the seed among them), the ``joined`` links in the order they were
    made, the named ``candidates`` and the ``undetermined`` units (key order). ``identified`` is False when the seed states
    no parcel and no address: the property is then the seed alone (R13)."""
    seed: Key
    members: tuple[Node, ...]
    joined: tuple[Joined, ...]
    candidates: tuple[Candidate, ...]
    undetermined: tuple[Node, ...]
    identified: bool

    @property
    def keys(self) -> frozenset[Key]:
        return frozenset(m.key for m in self.members)

    def reasons(self) -> dict[Key, str]:
        """Each joined member's link reason."""
        return {j.node.key: j.link.reason for j in self.joined}


@dataclass(frozen=True)
class Group:
    """A set of units one timeline treats as one property: a stable ``id``, its ``members`` and a short ``label``."""
    id: str
    members: tuple[Node, ...]
    label: str


@dataclass(frozen=True)
class Partition:
    """Every node of the graph in exactly one group. A group is found by its unit's key, or by the version a datum names
    (the timeline's datums carry the version they were read from, not the document's current one)."""
    groups: tuple[Group, ...]
    _by_key: Mapping[Key, Group]
    _by_version: Mapping[tuple[str, int], Group]
    _of_version: Mapping[str, tuple[Group, ...]]

    def group_of(self, key: Key) -> Group | None:
        return self._by_key.get(key)

    def group_of_unit(self, version_id: UUID | str, unit_no: int) -> Group | None:
        return self._by_version.get((str(version_id), unit_no))

    def groups_of_version(self, version_id: UUID | str) -> tuple[Group, ...]:
        """The distinct groups of a version's units, in group order."""
        return self._of_version.get(str(version_id), ())


def label_of(members: Sequence[Node]) -> str:
    """A short description of the property a group is: its address (with the city when the unit states one), else its
    block and parcels. Read from the first member that states any."""
    for n in sorted(members, key=lambda x: x.key):
        i = n.identity
        if i.addresses:
            where = " · ".join(f"{s} {num}" for s, num in sorted(i.addresses))
            return f"{where}, {i.link_city}" if i.link_city else where
        if i.parcels:
            by_block: dict[str, set[str]] = {}
            for block, parcel, sub in i.parcels:
                by_block.setdefault(block, set()).add(f"{parcel} תת-חלקה {sub}" if sub else parcel)
            return " · ".join(
                f"גוש {b} " + (f"חלקה {ps[0]}" if len(ps) == 1 else "חלקות " + ", ".join(ps))
                for b, ps in ((b, sorted(v, key=lambda x: (len(x), x))) for b, v in sorted(by_block.items())))
    return ""


class PropertyGraph:
    """The visible subject units of a turn with the relation between any two (see the module doc). Pure: built from nodes."""

    def __init__(self, nodes: Iterable[Node]) -> None:
        self.nodes: tuple[Node, ...] = tuple(sorted(nodes, key=lambda n: n.key))
        self._by_key = {n.key: n for n in self.nodes}
        self._by_version = {(str(n.version_id), n.unit_no): n for n in self.nodes}
        self._links: dict[tuple[Key, Key], Link] = {}
        self._parcel_maps = {n.key: _parcels(n.identity) for n in self.nodes}
        self._by_parcel: dict[tuple[str, str], list[Node]] = {}
        self._by_address: dict[tuple[str, str], list[Node]] = {}
        for n in self.nodes:
            for block, parcel, _ in n.identity.parcels:
                bucket = self._by_parcel.setdefault((block, parcel), [])
                if not bucket or bucket[-1] is not n:
                    bucket.append(n)
            for address in n.identity.addresses:
                self._by_address.setdefault(address, []).append(n)
        self._gathered: dict[Key, Gathered] = {}
        self._partitions: dict[frozenset | None, Partition] = {}

    def node(self, key: Key) -> Node | None:
        return self._by_key.get(key)

    def find(self, version_id: UUID | str, unit_no: int) -> Node | None:
        """The node of a unit by its version (what a unit handle names), or None when the unit is not in the graph."""
        return self._by_version.get((str(version_id), unit_no))

    def link(self, a: Key, b: Key) -> Link:
        """The relation of two nodes (cached; symmetric)."""
        pair = (a, b) if a <= b else (b, a)
        found = self._links.get(pair)
        if found is None:
            found = self._links[pair] = relate(self._by_key[pair[0]].identity, self._by_key[pair[1]].identity,
                                               self._parcel_maps[pair[0]], self._parcel_maps[pair[1]])
        return found

    def _neighbours(self, n: Node) -> list[tuple[Node, Link]]:
        """The units that share a parcel or an address with ``n`` and are gathered-linked to it, in (strength, key) order."""
        seen: dict[Key, Node] = {}
        for block, parcel, _ in n.identity.parcels:
            seen.update({m.key: m for m in self._by_parcel[(block, parcel)]})
        for address in n.identity.addresses:
            seen.update({m.key: m for m in self._by_address[address]})
        seen.pop(n.key, None)
        linked = [(m, self.link(n.key, m.key)) for m in seen.values()]
        return sorted(((m, link) for m, link in linked if link.gathered), key=lambda x: (STRENGTH[x[1].relation], x[0].key))

    def gather(self, seed: Key) -> Gathered:
        """The compatible closure from ``seed`` (the module doc) with its candidates and undetermined units."""
        found = self._gathered.get(seed)
        if found is None:
            found = self._gathered[seed] = self._gather(seed)
        return found

    def _gather(self, seed: Key) -> Gathered:
        root = self._by_key[seed]
        order: list[Node] = [root]
        members = {root.key}
        joined: list[Joined] = []
        queue = deque([root])
        while queue:
            current = queue.popleft()
            for n, link in self._neighbours(current):
                if n.key in members or not all(self.link(n.key, m.key).compatible for m in order):
                    continue
                order.append(n)
                members.add(n.key)
                joined.append(Joined(n, current.key, link))
                queue.append(n)
        candidates, undetermined = [], []
        for n in self.nodes:
            if n.key in members:
                continue
            links = [(m, self.link(m.key, n.key)) for m in order]
            if any(not link.compatible for _, link in links):
                continue  # proven to be another property
            named = next(((m, link) for m, link in links if link.relation == CANDIDATE), None)
            if named is not None:
                candidates.append(Candidate(n, named[1], named[0].key))
            elif not all(link.reason == REASON_DIFFERENT_ADDRESS for _, link in links):
                undetermined.append(n)
        return Gathered(seed, tuple(sorted(order, key=lambda x: x.key)), tuple(joined), tuple(candidates),
                        tuple(undetermined), root.identity.identified)

    def partition(self, focus: Gathered | None = None) -> Partition:
        """Every node in one group: the focus property's members as one group, and the others grouped without a seed (the
        module doc)."""
        cache_key = focus.keys if focus is not None else None
        found = self._partitions.get(cache_key)
        if found is None:
            found = self._partitions[cache_key] = self._partition(focus)
        return found

    def _partition(self, focus: Gathered | None) -> Partition:
        taken = set(focus.keys) if focus is not None else set()
        sets: list[list[Node]] = [list(focus.members)] if focus is not None else []
        rest = {n.key: n for n in self.nodes if n.key not in taken}
        done: set[Key] = set()
        for n in self.nodes:
            if n.key not in rest or n.key in done:
                continue
            component, stack = {n.key: n}, [n]
            while stack:  # the gathered-link component among the units outside the focus set
                for m, _ in self._neighbours(stack.pop()):
                    if m.key in rest and m.key not in component:
                        component[m.key] = m
                        stack.append(m)
            done |= component.keys()
            ordered = [component[k] for k in sorted(component)]
            if all(self.link(a.key, b.key).compatible for i, a in enumerate(ordered) for b in ordered[i + 1:]):
                sets.append(ordered)
            else:
                sets += [[m] for m in ordered]
        groups, by_key, by_version, of_version = [], {}, {}, {}
        for members in sets:
            ordered = tuple(sorted(members, key=lambda x: x.key))
            group = Group("{}:{}".format(*ordered[0].key), ordered, label_of(ordered))
            groups.append(group)
            for m in ordered:
                by_key[m.key] = group
                by_version[(str(m.version_id), m.unit_no)] = group
                held = of_version.setdefault(str(m.version_id), [])
                if group not in held:
                    held.append(group)
        return Partition(tuple(groups), by_key, by_version, {v: tuple(g) for v, g in of_version.items()})


# --- the visible units -----------------------------------------------------------------------------------------------

def _unpadded(parcels: Iterable, addresses: Iterable) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """Block/parcel pairs and street/number pairs without leading zeros and with the street's spaces collapsed."""
    return ({(unpad(str(b)), unpad(str(p))) for b, p in parcels},
            {(" ".join(str(s).split()), unpad(str(n).strip())) for s, n in addresses})


def _stored_sets(identifiers: Mapping | None) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """(the block/parcel pairs, the street/number pairs) of a unit's stored identifiers (``document_units.identifiers``),
    normalized the way the identity reader writes them (no leading zeros). The stored lists win; a unit that has only its
    ``shown`` strings (an older row) is read from them."""
    stored = identifiers or {}
    parcels, addresses = _unpadded(stored.get("parcels") or (), stored.get("addresses") or ())
    if not parcels and not addresses and stored.get("shown"):
        ident = contexts.identifiers("\n".join(str(x) for x in stored["shown"]))
        parcels, addresses = _unpadded(ident.parcels, ident.addresses)
    return parcels, addresses


def with_stored(found: UnitIdentity, identifiers: Mapping | None) -> UnitIdentity:
    """The unit's identity with its stored title identifiers as a fallback source of the same facts: only for a kind of fact
    (parcels, addresses) the identification reader found none of. A stored parcel carries no sub-parcel and no city."""
    parcels, addresses = _stored_sets(identifiers)
    return replace(found, parcels=found.parcels or frozenset((b, p, None) for b, p in parcels),
                   addresses=found.addresses or frozenset(addresses))


def current_appraisal_files(conn: Connection, document_ids: Iterable | None = None) -> list:
    """(``document_id``, ``version_id``) of the current version of every visible appraisal, in document order; only of
    ``document_ids`` when they are given."""
    named = document_ids is not None
    return conn.execute(text(
        "SELECT d.id AS document_id, v.id AS version_id FROM documents d JOIN document_versions v ON v.document_id = d.id"
        f" AND v.is_current WHERE d.deleted_at IS NULL AND {APPRAISALS_SQL}" + (" AND d.id = ANY(:d)" if named else "")
        + " ORDER BY d.id"), {"d": list(document_ids)} if named else {}).all()


def subject_units(conn: Connection, files: Sequence, cache: dict | None = None) -> list[tuple]:
    """(file, unit) of every subject unit — never an appendix or a comparables part — of the given files, in file and unit
    order. The units ``cache`` (the turn's dict: the keys ``dates.units_of`` reads) already holds are not read again; the
    stored units of the others are read in one query; a file whose units were not stored yet is read through
    ``units.ensure``, which stores them."""
    cache = {} if cache is None else cache
    unread = [f.version_id for f in files if ("units", f.version_id) not in cache]
    for v, stored in unit_store.stored_many(conn, unread).items():
        cache[("units", v)] = tuple(stored)
    found = []
    for f in files:
        found += [(f, u) for u in units_of(conn, f.version_id, cache) if u.kind not in contexts.NON_SUBJECT_KINDS]
    return found


def build_graph(conn: Connection, *, cache: dict | None = None, document_ids: Iterable | None = None) -> PropertyGraph:
    """The graph of the visible, current subject units (of ``document_ids`` only when given), under the caller's
    permissions. Identities come from ``identity.identities`` (derived on first use, ``cache`` is the turn's dict); a
    unit the identity reader does not count as a subject (front matter) is not a node."""
    cache = {} if cache is None else cache
    files = current_appraisal_files(conn, document_ids)
    found = identity.identities(conn, [f.version_id for f in files], cache=cache)
    nodes = []
    for f, u in subject_units(conn, files, cache):
        who = found.get(f.version_id, {}).get(u.unit_no)
        if who is not None:
            nodes.append(Node(f.document_id, f.version_id, u.unit_no, with_stored(who, u.identifiers), u.title))
    return PropertyGraph(nodes)
