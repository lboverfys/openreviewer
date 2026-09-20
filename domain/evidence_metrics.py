"""引用自动核验的原因分类，不把来源故障或规则未覆盖算成模型误报。"""

EVIDENCE_REASON_GROUPS = {
    "matched": frozenset({"exact_text_match", "identifier_overlap"}),
    "unmatched": frozenset({"line_range_out_of_bounds", "empty_evidence", "evidence_text_not_found"}),
    "infrastructure": frozenset({"source_readback_failed", "source_blob_unavailable", "source_blob_hash_mismatch", "blob_sha_mismatch"}),
    "not_covered": frozenset({"left_side_requires_base_blob", "blob_batch_limit", "source_unit_missing", "installation_id_missing",
        "finding_has_no_location", "verification_result_missing", "verification_reason_missing", "not_checked", "location_not_in_diff"}),
}


def evidence_category(status: str | None, reason: str | None) -> str:
    if status == "verified":
        return "matched" if reason in EVIDENCE_REASON_GROUPS["matched"] else "unclassified"
    if status not in {None, "unverified"}:
        return "unclassified"
    for category in ("unmatched", "infrastructure", "not_covered"):
        if reason in EVIDENCE_REASON_GROUPS[category] and (category != "unmatched" or status == "unverified"):
            return category
    return "unclassified"
