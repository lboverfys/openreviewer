"""请求费用依据使用本地合成账本验证，不连接服务商。"""

from decimal import Decimal

from sqlalchemy import select

from domain.model_review import ModelTokenUsage
from persistence.models import ModelUsageRequestRecord, RepositoryUsageMonthRecord
from persistence.usage_queries import UsageQueries
from services.model_review import ModelPricing
from services.usage import MonthlyModelAccountant
from tests.integration.test_management_api import database as database
from tests.integration.test_team_platform import ALL, request, setup_ledger


def test_missing_cache_price_keeps_usage_and_reserved_cost_with_immutable_rates(database):
    _, lease, _, ledger = setup_ledger(database)
    accountant = MonthlyModelAccountant(ledger, lambda: lease, "logic")
    prices = ModelPricing(Decimal("2"), Decimal("10")).snapshot()
    reservation = accountant.reserve(request(100, pricing_snapshot=prices))
    prices["input_usd_per_million"] = "99"
    usage = ModelTokenUsage(input_tokens=10, output_tokens=3, cache_read_input_tokens=5)
    accountant.settle(reservation, input_tokens=15, output_tokens=3, estimated_cost_microusd=None,
                     response_status=200, duration_ms=12, cost_reason="cache_price_missing", usage_details=usage)
    # 重复结束不能覆盖证据、改变原金额或重复扣减预占。
    accountant.settle(reservation, input_tokens=0, output_tokens=0, estimated_cost_microusd=0,
                     response_status=200, duration_ms=1)
    queries = UsageQueries(database.sessions)
    month = queries.months(ALL, "2026-09").items[0]
    row = queries.requests(ALL, month.id).items[0]
    assert row.status == "settled" and row.usage_status == "recorded"
    assert row.cost_status == "unknown" and row.cost_reason == "cache_price_missing"
    assert row.usage_details == usage
    assert row.pricing_snapshot["input_usd_per_million"] == "2"
    assert row.estimated_cost_microusd is None
    assert month.unknown_count == 1 and month.uncertain_count == 0
    assert month.reserved_cost_microusd == 100
    group = queries.breakdown(ALL, month.id)[0]
    assert group.missing_price_count == 1 and group.missing_usage_count == 0


def test_accounting_statuses_and_reason_totals_keep_old_records_honest(database):
    _, lease, _, ledger = setup_ledger(database)
    pending = ledger.reserve(lease, "logic", request())
    missing = ledger.reserve(lease, "logic", request())
    ledger.settle(missing, input_tokens=None, output_tokens=None, estimated_cost_microusd=None,
                  response_status=200, duration_ms=1, uncertain=True, cost_reason="usage_missing")
    partial = ledger.reserve(lease, "logic", request())
    ledger.settle(partial, input_tokens=10, output_tokens=2, estimated_cost_microusd=20,
                  response_status=200, duration_ms=1, uncertain=True, cost_reason="incomplete_response",
                  usage_details=ModelTokenUsage(input_tokens=10, output_tokens=2))
    old = ledger.reserve(lease, "logic", request())
    with database.sessions() as session, session.begin():
        legacy = session.get(ModelUsageRequestRecord, old.id)
        legacy.status, legacy.cost_reason = "settled", None
        legacy.input_tokens, legacy.output_tokens = 10, 2
    queries = UsageQueries(database.sessions)
    month = queries.months(ALL, "2026-09").items[0]
    rows = {row.id: row for row in queries.requests(ALL, month.id).items}
    assert (rows[pending.id].usage_status, rows[pending.id].cost_status, rows[pending.id].cost_reason) == ("pending", "pending", "pending")
    assert (rows[missing.id].usage_status, rows[missing.id].cost_status) == ("missing", "unknown")
    assert (rows[partial.id].usage_status, rows[partial.id].cost_status) == ("partial", "partial")
    assert rows[old.id].cost_reason == "legacy_unknown" and rows[old.id].pricing_snapshot is None
    assert rows[old.id].estimated_cost_microusd is None
    group = queries.breakdown(ALL, month.id)[0]
    assert group.unknown_count == group.reserved_count + group.missing_usage_count + group.missing_price_count + group.legacy_unknown_count == 3
    assert group.partial_cost_count == 1
    with database.sessions() as session:
        assert session.get(ModelUsageRequestRecord, old.id).cost_reason is None


def test_complete_accounting_preserves_token_categories_and_releases_only_its_reservation(database):
    _, lease, _, ledger = setup_ledger(database)
    pricing = ModelPricing(Decimal("2"), Decimal("10"), Decimal("0.5"), Decimal("3"))
    usage = ModelTokenUsage(input_tokens=100, output_tokens=20,
                           cache_read_input_tokens=40, cache_write_input_tokens=10, reasoning_output_tokens=5)
    accountant = MonthlyModelAccountant(ledger, lambda: lease, "logic")
    reservation = accountant.reserve(request(1000, pricing_snapshot=pricing.snapshot()))
    accountant.reserve(request(60))
    accountant.settle(reservation, input_tokens=usage.total_input_tokens, output_tokens=usage.output_tokens,
                     estimated_cost_microusd=pricing.estimate_microusd(usage), response_status=200,
                     duration_ms=1, usage_details=usage)
    with database.sessions() as session:
        month = session.scalar(select(RepositoryUsageMonthRecord))
        assert month.estimated_cost_microusd == 450
        assert month.reserved_cost_microusd == 60
        row = UsageQueries(database.sessions).requests(ALL, month.id).items
    completed = next(item for item in row if item.id == reservation.id)
    assert completed.cost_status == "estimated" and completed.cost_reason is None
    assert completed.usage_status == "recorded" and completed.usage_details == usage
    assert completed.pricing_snapshot == pricing.snapshot()
