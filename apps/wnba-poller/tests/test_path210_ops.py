from __future__ import annotations

import pytest

from wnba_poller.path210_ops import (
    apply_event_block,
    content_hash,
    get_event_block,
    render_event_block,
    validate_event_change,
)

GAME = {
    "event_id": "evt-1",
    "away_team": "Indiana Fever",
    "home_team": "Las Vegas Aces",
}

OUTPUT = {
    "full_game": {
        "side": {
            "selection": "Las Vegas Aces",
            "strength": "moderate",
            "evidence": ["Line moved toward Aces"],
            "watch_conditions": ["Injury report"],
        },
        "total": {
            "selection": "Over",
            "strength": "small",
            "evidence": ["High pace matchup"],
            "watch_conditions": [],
        },
    },
    "first_half": {},
    "summary": "Aces favored with room to grow.",
}


class TestRenderEventBlock:
    def test_renders_active_block_with_markers(self) -> None:
        block = render_event_block(
            game=GAME, revision_id="rev-1", status="active", output=OUTPUT
        )
        assert block.startswith(
            "<!-- WNBA_LEAN_EVENT_START event_id=evt-1 -->"
        )
        assert block.endswith("<!-- WNBA_LEAN_EVENT_END event_id=evt-1 -->")
        assert "Las Vegas Aces" in block
        assert "Aces favored with room to grow." in block

    def test_deleted_block_has_no_output_lines(self) -> None:
        block = render_event_block(
            game=GAME, revision_id="rev-2", status="deleted", output=None
        )
        assert "deleted through append-only revision history" in block
        assert "Aces favored" not in block

    def test_active_block_requires_output(self) -> None:
        with pytest.raises(ValueError, match="requires lean output"):
            render_event_block(
                game=GAME, revision_id="rev-1", status="active", output=None
            )

    def test_rejects_invalid_status(self) -> None:
        with pytest.raises(ValueError, match="active or deleted"):
            render_event_block(
                game=GAME, revision_id="rev-1", status="bogus", output=OUTPUT
            )

    def test_rejects_invalid_event_id(self) -> None:
        with pytest.raises(ValueError, match="invalid event_id"):
            render_event_block(
                game={**GAME, "event_id": "not valid!"},
                revision_id="rev-1",
                status="active",
                output=OUTPUT,
            )


class TestApplyAndGetEventBlock:
    def test_create_appends_new_block_to_empty_document(self) -> None:
        block = render_event_block(
            game=GAME, revision_id="rev-1", status="active", output=OUTPUT
        )
        document = "# path210\n\nSome curated rules.\n"
        after = apply_event_block(
            document, event_id="evt-1", operation="create", new_block=block
        )
        assert get_event_block(after, "evt-1") == block
        assert "Some curated rules." in after

    def test_create_rejects_when_block_already_exists(self) -> None:
        block = render_event_block(
            game=GAME, revision_id="rev-1", status="active", output=OUTPUT
        )
        document = "# path210\n\n" + block + "\n"
        with pytest.raises(ValueError, match="already exists"):
            apply_event_block(
                document, event_id="evt-1", operation="create", new_block=block
            )

    def test_revise_replaces_only_the_target_block(self) -> None:
        block_a = render_event_block(
            game=GAME, revision_id="rev-1", status="active", output=OUTPUT
        )
        other_game = {**GAME, "event_id": "evt-2", "home_team": "Chicago Sky"}
        block_b = render_event_block(
            game=other_game, revision_id="rev-9", status="active", output=OUTPUT
        )
        document = f"# path210\n\n{block_a}\n\n{block_b}\n"
        revised = render_event_block(
            game=GAME,
            revision_id="rev-2",
            status="active",
            output={**OUTPUT, "summary": "Updated."},
        )
        after = apply_event_block(
            document, event_id="evt-1", operation="revise", new_block=revised
        )
        assert get_event_block(after, "evt-1") == revised
        assert get_event_block(after, "evt-2") == block_b

    def test_revise_rejects_when_block_missing(self) -> None:
        block = render_event_block(
            game=GAME, revision_id="rev-1", status="active", output=OUTPUT
        )
        with pytest.raises(ValueError, match="does not exist"):
            apply_event_block(
                "# path210\n", event_id="evt-1", operation="revise", new_block=block
            )

    def test_rejects_unsupported_operation(self) -> None:
        block = render_event_block(
            game=GAME, revision_id="rev-1", status="active", output=OUTPUT
        )
        with pytest.raises(ValueError, match="unsupported"):
            apply_event_block(
                "# path210\n",
                event_id="evt-1",
                operation="rename",
                new_block=block,
            )

    def test_get_event_block_returns_none_when_absent(self) -> None:
        assert get_event_block("# path210\n", "evt-1") is None

    def test_get_event_block_rejects_duplicates(self) -> None:
        block = render_event_block(
            game=GAME, revision_id="rev-1", status="active", output=OUTPUT
        )
        document = f"{block}\n\n{block}\n"
        with pytest.raises(ValueError, match="duplicate"):
            get_event_block(document, "evt-1")


class TestValidateEventChange:
    def test_accepts_matching_create(self) -> None:
        block = render_event_block(
            game=GAME, revision_id="rev-1", status="active", output=OUTPUT
        )
        before = "# path210\n\nrules\n"
        after = apply_event_block(
            before, event_id="evt-1", operation="create", new_block=block
        )
        validate_event_change(
            before, after, event_id="evt-1", expected_block=block
        )

    def test_rejects_when_resulting_block_does_not_match_expected(self) -> None:
        block = render_event_block(
            game=GAME, revision_id="rev-1", status="active", output=OUTPUT
        )
        before = "# path210\n\nrules\n"
        after = apply_event_block(
            before, event_id="evt-1", operation="create", new_block=block
        )
        wrong_expected = block.replace("Aces favored", "Something else")
        with pytest.raises(ValueError, match="does not match expected output"):
            validate_event_change(
                before, after, event_id="evt-1", expected_block=wrong_expected
            )

    def test_rejects_unrelated_content_drift(self) -> None:
        block = render_event_block(
            game=GAME, revision_id="rev-1", status="active", output=OUTPUT
        )
        before = "# path210\n\nrules\n"
        after = apply_event_block(
            before, event_id="evt-1", operation="create", new_block=block
        )
        tampered = after.replace("rules", "tampered rules")
        with pytest.raises(
            ValueError, match="modified content outside target block"
        ):
            validate_event_change(
                before, tampered, event_id="evt-1", expected_block=block
            )

    def test_accepts_revise_leaving_unrelated_blocks_untouched(self) -> None:
        block_a = render_event_block(
            game=GAME, revision_id="rev-1", status="active", output=OUTPUT
        )
        other_game = {**GAME, "event_id": "evt-2", "home_team": "Chicago Sky"}
        block_b = render_event_block(
            game=other_game, revision_id="rev-9", status="active", output=OUTPUT
        )
        before = f"# path210\n\n{block_a}\n\n{block_b}\n"
        revised = render_event_block(
            game=GAME,
            revision_id="rev-2",
            status="active",
            output={**OUTPUT, "summary": "Updated."},
        )
        after = apply_event_block(
            before, event_id="evt-1", operation="revise", new_block=revised
        )
        validate_event_change(
            before, after, event_id="evt-1", expected_block=revised
        )


class TestContentHash:
    def test_is_deterministic_and_sensitive_to_content(self) -> None:
        assert content_hash("abc") == content_hash("abc")
        assert content_hash("abc") != content_hash("abd")
