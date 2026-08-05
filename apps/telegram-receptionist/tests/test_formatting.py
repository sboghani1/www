from receptionist.formatting import split_message


def test_split_message_preserves_content() -> None:
    text = "\n".join(f"line {index}" for index in range(1000))
    chunks = split_message(text, limit=200)
    assert all(len(chunk) <= 200 for chunk in chunks)
    assert "\n".join(chunks) == text

