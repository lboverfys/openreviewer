import json
from hashlib import sha1
from types import SimpleNamespace

import pytest

from domain.enums import (
    EvidenceVerificationStatus,
    FindingAdjudicationStatus,
    LocationSide,
    evidence_verification_status_for,
)
from services.evidence_verification import (
    GitHubEvidenceVerifier,
    verify_source_evidence,
)


@pytest.mark.parametrize(
    ("adjudication", "expected"),
    [
        (FindingAdjudicationStatus.UNREVIEWED, EvidenceVerificationStatus.UNVERIFIED),
        (FindingAdjudicationStatus.VALID, EvidenceVerificationStatus.VERIFIED),
        (FindingAdjudicationStatus.FALSE_POSITIVE, EvidenceVerificationStatus.REJECTED),
        (FindingAdjudicationStatus.DUPLICATE, EvidenceVerificationStatus.NOT_APPLICABLE),
        (FindingAdjudicationStatus.OUT_OF_SCOPE, EvidenceVerificationStatus.NOT_APPLICABLE),
        (FindingAdjudicationStatus.KNOWN_ISSUE, EvidenceVerificationStatus.NOT_APPLICABLE),
    ],
)
def test_evidence_verification_is_independent_from_location(
    adjudication: FindingAdjudicationStatus,
    expected: EvidenceVerificationStatus,
) -> None:
    assert evidence_verification_status_for(adjudication) is expected


def _git_blob_sha(source: str) -> str:
    raw = source.encode("utf-8")
    return sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()


def test_verify_source_evidence_requires_git_blob_identity_and_exact_lines() -> None:
    source = "def authorize(user):\n    return user.is_admin\n"
    blob_sha = _git_blob_sha(source)

    verified = verify_source_evidence(
        source,
        blob_sha=blob_sha,
        expected_blob_sha=blob_sha,
        start_line=2,
        end_line=2,
        side=LocationSide.RIGHT,
        evidence="return user.is_admin",
    )
    assert verified.status is EvidenceVerificationStatus.VERIFIED
    assert verified.matched_lines == (2,)

    mismatched = verify_source_evidence(
        source,
        blob_sha="0" * 40,
        expected_blob_sha=blob_sha,
        start_line=2,
        end_line=2,
        side=LocationSide.RIGHT,
        evidence="return user.is_admin",
    )
    assert mismatched.status is EvidenceVerificationStatus.UNVERIFIED
    assert mismatched.reason == "blob_sha_mismatch"


def test_verify_source_evidence_rejects_left_side_and_out_of_range() -> None:
    source = "line one\nline two\n"
    blob_sha = _git_blob_sha(source)
    left = verify_source_evidence(
        source,
        blob_sha=blob_sha,
        expected_blob_sha=blob_sha,
        start_line=1,
        end_line=1,
        side=LocationSide.LEFT,
        evidence="line one",
    )
    out_of_range = verify_source_evidence(
        source,
        blob_sha=blob_sha,
        expected_blob_sha=blob_sha,
        start_line=3,
        end_line=3,
        side=LocationSide.RIGHT,
        evidence="line three",
    )
    assert left.reason == "left_side_requires_base_blob"
    assert out_of_range.reason == "line_range_out_of_bounds"


class _StaticTokens:
    def __init__(self) -> None:
        self.installations: list[int] = []

    def get_token(self, installation_id: int) -> str:
        self.installations.append(installation_id)
        return "installation-token"


class _GraphqlApi:
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.calls: list[tuple[str, str, object]] = []

    def request_json(self, method: str, path: str, **kwargs: object) -> SimpleNamespace:
        self.calls.append((method, path, kwargs.get("json_body")))
        return SimpleNamespace(payload=self.payload)


def test_graphql_blob_loader_checks_repository_identity_and_uses_installation_token() -> None:
    source = "def authorize():\n    return True\n"
    blob_sha = _git_blob_sha(source)
    payload = {
        "data": {
            "repository": {
                "databaseId": 42,
                "nameWithOwner": "owner/repository",
                "blob0": {
                    "__typename": "Blob",
                    "oid": blob_sha,
                    "byteSize": len(source.encode("utf-8")),
                    "isBinary": False,
                    "text": source,
                },
            }
        }
    }
    api = _GraphqlApi(payload)
    tokens = _StaticTokens()
    verifier = GitHubEvidenceVerifier(api, tokens)
    loaded = verifier._load_blobs(
        "owner/repository",
        42,
        "a" * 40,
        (("src/auth.py", blob_sha),),
        tokens.get_token(77),
    )
    assert loaded == {("src/auth.py", blob_sha): source}
    assert tokens.installations == [77]
    assert len(api.calls) == 1
    body = api.calls[0][2]
    assert isinstance(body, dict)
    assert "query" in body and "variables" in body
    json.dumps(body, ensure_ascii=False)

    api.payload = {
        "data": {
            "repository": {
                "databaseId": 99,
                "nameWithOwner": "owner/repository",
            }
        }
    }
    with pytest.raises(ValueError, match="identity mismatch"):
        verifier._load_blobs(
            "owner/repository",
            42,
            "a" * 40,
            (("src/auth.py", blob_sha),),
            "installation-token",
        )
