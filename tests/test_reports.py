import pytest

from retail_analyst.reports import ReportStore


@pytest.fixture
def store(tmp_path):
    return ReportStore(tmp_path / "reports.db")


def test_find_by_mention_is_case_insensitive(store):
    store.save("ana", "c1", "Q1 review", "Levi's grew 10%")
    store.save("ana", "c1", "Nike deep dive", "Nike fell")
    assert [r.title for r in store.find("ana", mentions="levi's")] == ["Q1 review"]


def test_find_by_conversation(store):
    store.save("ana", "c1", "A", "x")
    store.save("ana", "c2", "B", "x")
    assert [r.title for r in store.find("ana", conversation_id="c2")] == ["B"]


def test_users_only_see_and_delete_their_own_reports(store):
    own = store.save("ana", "c1", "Mine", "x")
    other = store.save("bob", "c9", "Not mine", "x")
    assert [r.id for r in store.list("ana")] == [own.id]
    assert store.delete("ana", [own.id, other.id]) == 1
    assert [r.id for r in store.list("bob")] == [other.id]
