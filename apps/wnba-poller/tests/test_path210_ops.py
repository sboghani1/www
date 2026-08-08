from __future__ import annotations

import pytest

from wnba_poller.path210_ops import (
    apply_event_block,
    apply_resolution_entry,
    content_hash,
    get_event_block,
    next_past_events_entry_number,
    rebuild_model_cache_counts,
    render_event_block,
    render_resolution_entry,
    validate_event_change,
    validate_model_cache_rebuild,
    validate_resolution_change,
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


def _resolution_document(*, entries: str = "", event_block: str = "") -> str:
    return (
        "# Notes For Model\n\nrules text\n"
        "\n# Past Events\n\n"
        f"{entries}"
        "\n# Model Cache\n\ncache stuff\n"
        "\n# Upcoming Events\n\n"
        f"{event_block}\n"
    )


class TestNextPastEventsEntryNumber:
    def test_increments_from_the_highest_existing_number(self) -> None:
        document = _resolution_document(
            entries="1fadesparks\nwrong\ntag\ncontext: x.\n\n5fadeaces\nright\ntag\ncontext: y.\n"
        )
        assert next_past_events_entry_number(document) == 6

    def test_starts_at_one_when_no_entries_exist(self) -> None:
        document = _resolution_document(entries="")
        assert next_past_events_entry_number(document) == 1

    def test_missing_past_events_section_raises(self) -> None:
        with pytest.raises(ValueError, match="Past Events"):
            next_past_events_entry_number("# Notes For Model\n\nno sections here\n")


class TestRenderResolutionEntry:
    def test_renders_all_fields_in_order(self) -> None:
        text = render_resolution_entry(
            entry_name="6fademercury",
            result="wrong",
            tags="back_favorite,follow_line_movement",
            line_movement="dream -5.5 (open) -> -7 (close)",
            context="context: wednesday. faded the dream cover.",
            model_lean="side (MERCURY +7) -- MISS.",
        )
        assert text == (
            "6fademercury\n"
            "wrong\n"
            "back_favorite,follow_line_movement\n"
            "line movement: dream -5.5 (open) -> -7 (close)\n"
            "context: wednesday. faded the dream cover.\n"
            "model_lean: side (MERCURY +7) -- MISS."
        )

    def test_line_movement_and_model_lean_are_optional(self) -> None:
        text = render_resolution_entry(
            entry_name="6fademercury",
            result="right",
            tags="tag",
            line_movement="",
            context="context: x.",
            model_lean="",
        )
        assert "line movement:" not in text
        assert "model_lean:" not in text

    def test_rejects_bad_entry_name(self) -> None:
        with pytest.raises(ValueError, match="invalid path210 resolution entry name"):
            render_resolution_entry(
                entry_name="fademercury",
                result="right",
                tags="tag",
                line_movement="",
                context="context: x.",
                model_lean="",
            )

    def test_rejects_bad_result(self) -> None:
        with pytest.raises(ValueError, match="right, wrong, or push"):
            render_resolution_entry(
                entry_name="6fademercury",
                result="maybe",
                tags="tag",
                line_movement="",
                context="context: x.",
                model_lean="",
            )

    def test_rejects_missing_tags(self) -> None:
        with pytest.raises(ValueError, match="requires tags"):
            render_resolution_entry(
                entry_name="6fademercury",
                result="right",
                tags="  ",
                line_movement="",
                context="context: x.",
                model_lean="",
            )

    def test_rejects_context_without_prefix(self) -> None:
        with pytest.raises(ValueError, match="must start with 'context:'"):
            render_resolution_entry(
                entry_name="6fademercury",
                result="right",
                tags="tag",
                line_movement="",
                context="wednesday, faded the dream.",
                model_lean="",
            )


class TestApplyAndValidateResolutionEntry:
    def _block(self) -> str:
        return render_event_block(
            game=GAME, revision_id="rev-1", status="active", output=OUTPUT
        )

    def _entry(self) -> str:
        return render_resolution_entry(
            entry_name="6fadeaces",
            result="right",
            tags="back_favorite",
            line_movement="aces -3 (close)",
            context="context: wednesday. faded the fever.",
            model_lean="side (ACES -3) -- HIT.",
        )

    def test_removes_block_and_appends_before_model_cache(self) -> None:
        block = self._block()
        before = _resolution_document(
            entries="1fadesparks\nwrong\ntag\ncontext: x.\n\n",
            event_block=block,
        )
        entry = self._entry()

        after = apply_resolution_entry(
            before, event_id="evt-1", entry_text=entry
        )

        assert get_event_block(after, "evt-1") is None
        assert entry in after
        # The new entry lands before Model Cache, i.e. within Past Events.
        assert after.index(entry) < after.index("# Model Cache")

    def test_rejects_when_block_is_missing(self) -> None:
        before = _resolution_document(entries="", event_block="")
        with pytest.raises(ValueError, match="does not exist"):
            apply_resolution_entry(
                before, event_id="evt-1", entry_text=self._entry()
            )

    def test_validate_accepts_clean_application(self) -> None:
        block = self._block()
        before = _resolution_document(
            entries="1fadesparks\nwrong\ntag\ncontext: x.\n\n",
            event_block=block,
        )
        entry = self._entry()
        after = apply_resolution_entry(
            before, event_id="evt-1", entry_text=entry
        )

        validate_resolution_change(
            before, after, event_id="evt-1", entry_text=entry
        )

    def test_validate_rejects_leftover_block(self) -> None:
        block = self._block()
        before = _resolution_document(
            entries="1fadesparks\nwrong\ntag\ncontext: x.\n\n",
            event_block=block,
        )
        entry = self._entry()
        # Simulate a bad apply that failed to remove the original block.
        after = before + "\n" + entry + "\n"

        with pytest.raises(ValueError, match="did not remove"):
            validate_resolution_change(
                before, after, event_id="evt-1", entry_text=entry
            )

    def test_validate_rejects_unrelated_drift(self) -> None:
        block = self._block()
        before = _resolution_document(
            entries="1fadesparks\nwrong\ntag\ncontext: x.\n\n",
            event_block=block,
        )
        entry = self._entry()
        after = apply_resolution_entry(
            before, event_id="evt-1", entry_text=entry
        )
        tampered = after.replace("rules text", "tampered rules text")

        with pytest.raises(ValueError, match="modified content outside"):
            validate_resolution_change(
                before, tampered, event_id="evt-1", entry_text=entry
            )


_CACHE_DOC = (
    "# Notes For Model\n\nRules mention '# Model Cache' here (must be ignored).\n"
    "\n# Past Events\n\n"
    "1fadea\nright\nback_favorite,total_over\ncontext: monday. a.\n\n"
    "2fadeb\nwrong\nback_favorite,total_under\ncontext: tuesday. b.\n\n"
    "3fadec\nwrong\ntotal_over,soccer,world_cup\ncontext: friday. c.\n\n"
    "# Model Cache\n\n"
    "Signal right/wrong record (based on tags):\n"
    "back_favorite: 0 right / 0 wrong\n"
    "total_over: 9 right / 9 wrong\n"
    "total_under: 0 right / 0 wrong\n"
    "  (prose annotation line -- left untouched)\n"
    "\n# Upcoming Events\n\nsome upcoming text\n"
)


class TestRebuildModelCacheCounts:
    def test_recomputes_counts_wnba_only(self) -> None:
        out = rebuild_model_cache_counts(_CACHE_DOC)
        # back_favorite: entry 1 right, entry 2 wrong -> 1/1
        assert "back_favorite: 1 right / 1 wrong" in out
        # total_over: entry 1 right; entry 3 excluded (soccer/world_cup) -> 1/0
        assert "total_over: 1 right / 0 wrong" in out
        # total_under: entry 2 wrong -> 0/1
        assert "total_under: 0 right / 1 wrong" in out

    def test_leaves_prose_and_other_sections_untouched(self) -> None:
        out = rebuild_model_cache_counts(_CACHE_DOC)
        assert "  (prose annotation line -- left untouched)" in out
        assert out.split("# Model Cache")[0] == _CACHE_DOC.split("# Model Cache")[0]
        assert "some upcoming text" in out.split("# Upcoming Events")[1]

    def test_is_idempotent(self) -> None:
        once = rebuild_model_cache_counts(_CACHE_DOC)
        assert rebuild_model_cache_counts(once) == once

    def test_does_not_match_inline_heading_mention(self) -> None:
        # The Notes section's quoted '# Model Cache' must not be treated as the
        # real heading (the bug that once wiped Past Events).
        out = rebuild_model_cache_counts(_CACHE_DOC)
        assert "1fadea" in out and "2fadeb" in out and "3fadec" in out


class TestValidateModelCacheRebuild:
    def test_accepts_a_clean_rebuild(self) -> None:
        after = rebuild_model_cache_counts(_CACHE_DOC)
        validate_model_cache_rebuild(_CACHE_DOC, after)

    def test_rejects_a_change_outside_the_cache(self) -> None:
        after = rebuild_model_cache_counts(_CACHE_DOC)
        tampered = after.replace("some upcoming text", "TAMPERED", 1)
        with pytest.raises(ValueError, match="after the Model Cache"):
            validate_model_cache_rebuild(_CACHE_DOC, tampered)

    def test_rejects_a_non_count_line_change(self) -> None:
        after = rebuild_model_cache_counts(_CACHE_DOC)
        tampered = after.replace("prose annotation line", "prose CHANGED", 1)
        with pytest.raises(ValueError, match="non-count line"):
            validate_model_cache_rebuild(_CACHE_DOC, tampered)

    def test_rejects_counts_inconsistent_with_tags(self) -> None:
        tampered = _CACHE_DOC.replace(
            "back_favorite: 0 right / 0 wrong",
            "back_favorite: 50 right / 0 wrong",
            1,
        )
        with pytest.raises(ValueError, match="not consistent"):
            validate_model_cache_rebuild(_CACHE_DOC, tampered)
