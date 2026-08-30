from datetime import timedelta

import pytest

from services.operations import OperationsSettings


def test_finding_evaluation_retention_has_a_conservative_default() -> None:
    settings = OperationsSettings.from_environment({})

    assert settings.finding_evaluation_retention == timedelta(days=730)


@pytest.mark.parametrize("value", ("29", "3651", "not-a-number"))
def test_finding_evaluation_retention_rejects_unsafe_values(value: str) -> None:
    with pytest.raises(ValueError):
        OperationsSettings.from_environment(
            {"OPENREVIEWER_FINDING_EVALUATION_RETENTION_DAYS": value}
        )
