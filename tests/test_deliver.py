from ytdigest.deliver import escape_markdown_v2

TRICKY_TITLE = "_ * [ ] ( ) ~ > # + - = | { } . !"


def test_markdown_v2_escaping_covers_all_special_chars():
    escaped = escape_markdown_v2(TRICKY_TITLE)
    for ch in "_*[]()~>#+-=|{}.!":
        assert f"\\{ch}" in escaped


def test_markdown_v2_escaping_round_trip_unescapes_cleanly():
    escaped = escape_markdown_v2(TRICKY_TITLE)
    # naive unescape: drop every backslash that precedes a special char
    unescaped = []
    i = 0
    while i < len(escaped):
        if escaped[i] == "\\" and i + 1 < len(escaped):
            unescaped.append(escaped[i + 1])
            i += 2
        else:
            unescaped.append(escaped[i])
            i += 1
    assert "".join(unescaped) == TRICKY_TITLE


def test_plain_text_untouched():
    assert escape_markdown_v2("Hello world 123") == "Hello world 123"
