from services.retrieval_context import review_queries
from tests.unit.test_model_review import make_model_input


def test_review_queries_cover_late_units_without_unbounded_query_count():
    source = make_model_input().units[0]
    units = tuple(source.model_copy(update={"file": f"Service{n}.java", "unit_key": f"{n:064x}", "group_key": None, "patch": f"@@ -1 +1 @@\n+findAccount{n}();"}) for n in range(24))
    queries = review_queries(units, "lexical_relations", 8)
    assert len(queries) == 8
    assert {key for _, keys in queries for key in keys} == {unit.unit_key for unit in units}
    assert "findaccount23" in queries[-1][0].query
    assert all(len(query.query) <= 3000 for query, _ in queries)
