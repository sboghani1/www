from receptionist.bot import (
    WNBA_CALLBACK_PATTERN,
    WNBA_GAMES_PAGE_SIZE,
    wnba_callback,
    wnba_games_header,
    wnba_games_markup,
    wnba_page_games,
)


def _games(count: int) -> list[dict]:
    return [
        {
            "event_id": f"evt-{index}",
            "away_team": f"Away {index}",
            "home_team": f"Home {index}",
            "commence_time_et": f"2026-08-{10 + index:02d}T19:00:00-04:00",
        }
        for index in range(count)
    ]


def test_page_size_is_five() -> None:
    assert WNBA_GAMES_PAGE_SIZE == 5


def test_page_games_splits_into_pages_of_five() -> None:
    games = _games(12)

    first, page, page_count = wnba_page_games(games, 0)
    assert [g["event_id"] for g in first] == [f"evt-{i}" for i in range(5)]
    assert page == 0
    assert page_count == 3

    second, page, page_count = wnba_page_games(games, 1)
    assert [g["event_id"] for g in second] == [f"evt-{i}" for i in range(5, 10)]
    assert page == 1

    last, page, page_count = wnba_page_games(games, 2)
    assert [g["event_id"] for g in last] == ["evt-10", "evt-11"]
    assert page == 2


def test_page_games_clamps_out_of_range_pages() -> None:
    games = _games(12)

    _, page, page_count = wnba_page_games(games, 99)
    assert page == page_count - 1 == 2

    _, page, _ = wnba_page_games(games, -1)
    assert page == 0


def test_page_games_on_empty_list_is_one_page() -> None:
    current, page, page_count = wnba_page_games([], 0)
    assert current == []
    assert page == 0
    assert page_count == 1


def test_games_header_shows_page_indicator() -> None:
    games = _games(12)
    assert wnba_games_header(games, 0) == (
        "🏀 WNBA games in the next 14 days\nPage 1 of 3"
    )
    assert wnba_games_header(games, 2) == (
        "🏀 WNBA games in the next 14 days\nPage 3 of 3"
    )


def test_games_header_empty() -> None:
    assert wnba_games_header([], 0) == "No upcoming WNBA games are available."


def test_markup_shows_exactly_five_game_buttons_on_full_page() -> None:
    games = _games(12)
    markup = wnba_games_markup(games, 0)
    game_rows = markup.inline_keyboard[:5]
    assert len(game_rows) == 5
    assert all(len(row) == 1 for row in game_rows)
    labels = [row[0].text for row in game_rows]
    assert "Away 0 @ Home 0" in labels[0]


def test_markup_first_page_has_only_next_button() -> None:
    games = _games(12)
    markup = wnba_games_markup(games, 0)
    nav_row = markup.inline_keyboard[-1]
    callbacks = [button.callback_data for button in nav_row]
    assert callbacks == [wnba_callback("page", "1")]


def test_markup_middle_page_has_prev_and_next() -> None:
    games = _games(12)
    markup = wnba_games_markup(games, 1)
    nav_row = markup.inline_keyboard[-1]
    callbacks = [button.callback_data for button in nav_row]
    assert callbacks == [
        wnba_callback("page", "0"),
        wnba_callback("page", "2"),
    ]


def test_markup_last_page_has_only_prev_button() -> None:
    games = _games(12)
    markup = wnba_games_markup(games, 2)
    nav_row = markup.inline_keyboard[-1]
    callbacks = [button.callback_data for button in nav_row]
    assert callbacks == [wnba_callback("page", "1")]


def test_markup_single_page_has_no_navigation_row() -> None:
    games = _games(3)
    markup = wnba_games_markup(games, 0)
    assert len(markup.inline_keyboard) == 3


def test_markup_game_buttons_carry_event_id_callback() -> None:
    games = _games(2)
    markup = wnba_games_markup(games, 0)
    assert markup.inline_keyboard[0][0].callback_data == wnba_callback(
        "game", "evt-0"
    )


def test_callback_pattern_matches_page_actions() -> None:
    assert WNBA_CALLBACK_PATTERN.match("wnba:page:0")
    assert WNBA_CALLBACK_PATTERN.match("wnba:page:1234")
    assert not WNBA_CALLBACK_PATTERN.match("wnba:page:")
    assert not WNBA_CALLBACK_PATTERN.match("wnba:page:12345")


def test_callback_pattern_matches_existing_actions() -> None:
    for data in (
        "wnba:game:evt-1",
        "wnba:generate",
        "wnba:copy",
        "wnba:history",
        "wnba:revisions",
        "wnba:undo",
    ):
        assert WNBA_CALLBACK_PATTERN.match(data), data


def test_callback_pattern_rejects_unknown_actions() -> None:
    assert not WNBA_CALLBACK_PATTERN.match("wnba:bogus")
    assert not WNBA_CALLBACK_PATTERN.match("wnba:")
