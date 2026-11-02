"""The identity an appendix or comparables part states in its own lead heading, through the stored units and blocks (round 19 U2,
KTD2, R5): ``contexts.of_version`` reads the file's heading blocks, picks the lead heading of each such part (``lead_heading_blocks``)
and gives its segment the labelled identifiers the heading writes (``Segment.lead_identity``). An appendix whose heading names another
address and parcel than the appraisal is another property; one whose heading names none is not. Every name and figure is invented."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from app.chat import contexts
from app.db import tenant_tx
from tests.conftest import login
from tests.factories import make_office
from tests.support.unit_files import READING, unit_file

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def fresh_caches():
    contexts._CACHE.clear()


@pytest.fixture
def office(db, client):
    a = make_office(db, "משרד ב", "admin-b@example.test")
    login(client, "admin-b@example.test")
    a.client = client
    return a


def file_with_appendix(office, heading: str) -> contexts.Contexts:
    """Unit 1: the appraisal of the invented «מתחם גבעת האלה» (street «הדס 5», parcel 40100/12), its first block a heading. Unit 2: an
    appendix with no identification of its own, whose first block (the lead heading of the unit) is ``heading``."""
    _, ver = unit_file(office, office.default_group_id, "קובץ שומה סינתטי", [
        {"title": "מתחם גבעת האלה", "aliases": ["גבעת האלה"], "identifiers": ["רחוב הדס 5", "גוש 40100 חלקה 12"],
         "blocks": ["שומת מקרקעין — מתחם גבעת האלה", "שווי הנכס הוא 1,000,000 ₪."]},
        {"title": None, "kind": "appendix", "blocks": [heading, "שווי 900,000 ₪ למ״ר."]}])
    found = contexts.identifiers("רחוב הדס 5\nגוש 40100 חלקה 12")  # the labelled identifiers the unit store keeps beside what was shown
    stored = {"addresses": [list(a) for a in found.addresses], "parcels": [list(p) for p in found.parcels], "shown": list(found.shown)}
    with tenant_tx(office.system) as conn:
        conn.execute(text("UPDATE document_blocks SET kind = 'heading' WHERE version_id = :v AND block_index = ANY(:b)"),
                     {"v": ver, "b": [0, 2]})
        conn.execute(text("UPDATE document_units SET identifiers = CAST(:i AS jsonb) WHERE version_id = :v AND unit_no = 1"),
                     {"v": ver, "i": json.dumps(stored, ensure_ascii=False)})
    with tenant_tx(office.system) as conn:
        return contexts.of_version(conn, ver, READING)


def test_an_appendix_whose_lead_heading_names_another_address_and_parcel_is_another_property(office):
    cx = file_with_appendix(office, "נספח א׳ — נכס השוואה ברחוב הדקל 8, גוש 40105 חלקה 2")
    assert cx.numbers == [1, 2] and cx.kind(2) == "appendix"
    lead = cx.segment(2).lead_identity
    assert not lead.empty and lead.parcels == {("40105", "2")}
    assert cx.segment(1).lead_identity.empty  # an appraisal unit's heading is read for no lead identity
    assert cx.compare(2, [1]) == contexts.DIFFERENT
    assert cx.compare(2, [1], complete=False) == contexts.UNKNOWN


def test_an_appendix_whose_lead_heading_names_no_address_or_parcel_is_not_another_property(office):
    cx = file_with_appendix(office, "נספח ב׳ — תחשיב")
    assert cx.segment(2).lead_identity.empty
    assert cx.compare(2, [1]) != contexts.DIFFERENT


def test_an_appendix_whose_lead_heading_names_the_asked_address_and_parcel_is_not_another_property(office):
    cx = file_with_appendix(office, "נספח ג׳ — תחשיב לנכס ברחוב הדס 5, גוש 40100 חלקה 12")
    assert not cx.segment(2).lead_identity.empty
    assert cx.compare(2, [1]) != contexts.DIFFERENT
