from __future__ import annotations

import pytest

from wnba_poller.grading import grade_lean, grade_side, grade_total


class TestGradeSide:
    def test_favorite_covers(self) -> None:
        assert (
            grade_side(
                away_team="Phoenix Mercury",
                home_team="Atlanta Dream",
                away_score=82,
                home_score=96,
                selection="Atlanta Dream",
                line=-7,
            )
            == "right"
        )

    def test_favorite_fails_to_cover(self) -> None:
        # Dream won by 14 (96-82); Mercury +7 needed to lose by < 7.
        assert (
            grade_side(
                away_team="Phoenix Mercury",
                home_team="Atlanta Dream",
                away_score=82,
                home_score=96,
                selection="Phoenix Mercury",
                line=7,
            )
            == "wrong"
        )

    def test_exact_push(self) -> None:
        # Home wins by exactly 7; home -7 is a push.
        assert (
            grade_side(
                away_team="Away",
                home_team="Home",
                away_score=80,
                home_score=87,
                selection="Home",
                line=-7,
            )
            == "push"
        )

    def test_underdog_covers_on_a_loss(self) -> None:
        # Away loses by 5 but got +7 points -- covers.
        assert (
            grade_side(
                away_team="Away",
                home_team="Home",
                away_score=80,
                home_score=85,
                selection="Away",
                line=7,
            )
            == "right"
        )

    def test_underdog_wins_outright(self) -> None:
        assert (
            grade_side(
                away_team="Away",
                home_team="Home",
                away_score=90,
                home_score=85,
                selection="Away",
                line=7,
            )
            == "right"
        )

    def test_rejects_selection_matching_neither_team(self) -> None:
        with pytest.raises(ValueError, match="does not match either team"):
            grade_side(
                away_team="Away",
                home_team="Home",
                away_score=80,
                home_score=85,
                selection="Somebody Else",
                line=7,
            )


class TestGradeTotal:
    def test_over_hits(self) -> None:
        assert (
            grade_total(away_score=82, home_score=96, selection="Over", line=181.5)
            == "wrong"
        )
        assert (
            grade_total(away_score=100, home_score=100, selection="Over", line=181.5)
            == "right"
        )

    def test_under_hits(self) -> None:
        assert (
            grade_total(away_score=82, home_score=96, selection="Under", line=181.5)
            == "right"
        )

    def test_exact_push(self) -> None:
        assert (
            grade_total(away_score=90, home_score=90, selection="Over", line=180)
            == "push"
        )
        assert (
            grade_total(away_score=90, home_score=90, selection="Under", line=180)
            == "push"
        )

    def test_rejects_invalid_selection(self) -> None:
        with pytest.raises(ValueError, match="Over or Under"):
            grade_total(away_score=80, home_score=85, selection="Push", line=180)


class TestGradeLean:
    GAME = {
        "away_team": "Phoenix Mercury",
        "home_team": "Atlanta Dream",
        "away_score": "82",
        "home_score": "96",
        "latest_away_spread": "7",
        "latest_home_spread": "-7",
        "latest_total": "181.5",
    }

    def test_grades_both_legs_using_the_real_mercury_dream_result(self) -> None:
        revision = {
            "full_game_side_selection": "Phoenix Mercury",
            "full_game_total_selection": "Over",
        }

        graded = grade_lean(game=self.GAME, active_revision=revision)

        assert graded["away_score"] == 82
        assert graded["home_score"] == 96
        assert graded["side"] == {
            "selection": "Phoenix Mercury",
            "line": 7.0,
            "result": "wrong",
        }
        assert graded["total"] == {
            "selection": "Over",
            "line": 181.5,
            "result": "wrong",
        }

    def test_missing_score_is_rejected_rather_than_invented(self) -> None:
        game = {**self.GAME, "away_score": "", "home_score": ""}
        with pytest.raises(ValueError, match="no recorded final score"):
            grade_lean(
                game=game,
                active_revision={"full_game_side_selection": "Phoenix Mercury"},
            )

    def test_missing_line_is_rejected_rather_than_invented(self) -> None:
        game = {**self.GAME, "latest_total": "nodata"}
        with pytest.raises(ValueError, match="no closing line"):
            grade_lean(
                game=game,
                active_revision={"full_game_total_selection": "Over"},
            )

    def test_no_side_selection_yields_none(self) -> None:
        graded = grade_lean(
            game=self.GAME,
            active_revision={"full_game_total_selection": "Over"},
        )
        assert graded["side"] is None
        assert graded["total"] is not None
