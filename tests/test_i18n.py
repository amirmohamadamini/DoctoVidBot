from __future__ import annotations

import copy

import pytest

from doctovid import i18n
from doctovid.i18n import FSI, LRM, PDI, RLM, Html
from doctovid.menus import KEYBOARD, keyboard_actions

from .conftest import EN, FA, LOCALES

REFERENCE = i18n.read("en")

#: Messages shown in a button's pop-up, which Telegram cuts off at 200
#: characters. Checked with long values, in every language.
ALERTS = {
    "menu.expired_alert": {},
    "queue.per_user": {"count": 100},
    "queue.full": {},
    "join.prompt": {},
    "join.not_yet": {},
    "admin.cache_off": {},
    "admin.cache_unset": {},
    "admin.join_unset": {},
    "channel.not_found": {},
    "channel.bot_missing": {},
}


@pytest.mark.parametrize("code", i18n.available())
def test_every_language_matches_english(code: str) -> None:
    assert i18n.problems(code, i18n.read(code), REFERENCE) == []


@pytest.mark.parametrize("code", i18n.available())
def test_pop_ups_fit(code: str) -> None:
    t = LOCALES.get(code)
    for key, values in ALERTS.items():
        assert len(t.alert(key, **values)) <= 200, key
    fix = t.phrase("channel.fix_members")
    assert len(i18n.plain(t.text("channel.not_allowed", fix=fix))) <= 200


@pytest.mark.parametrize("code", i18n.available())
def test_every_keyboard_button_is_its_own(code: str) -> None:
    labels = [LOCALES.get(code).label(f"keyboard.{action}") for action in KEYBOARD]
    assert len(set(labels)) == len(labels)


def test_no_label_means_two_things_in_two_languages() -> None:
    actions = keyboard_actions(LOCALES)
    assert len(actions) == len(KEYBOARD) * len(i18n.available())


# ------------------------------------------------------------ the checker


def broken(**changes: object) -> dict[str, object]:
    data = copy.deepcopy(i18n.read("fa"))
    for dotted, value in changes.items():
        section, _, key = dotted.partition("__")
        table = data[section]
        assert isinstance(table, dict)
        if value is None:
            del table[key]
        else:
            table[key] = value
    return data


@pytest.mark.parametrize(
    ("changes", "problem"),
    [
        ({"menu__cancel": None}, "fa: missing menu.cancel"),
        ({"menu__cancle": "x"}, "fa: menu.cancle isn't used"),
        ({"menu__title": "<b>{nam}</b> · {size}"}, "fa: menu.title must use ['name', 'size']"),
        ({"menu__title": "{name} · {size}"}, "fa: menu.title must use the tags ['</b>', '<b>']"),
        ({"usage__files": "{count} فایل"}, "fa: usage.files must have plural forms"),
        ({"usage__files": {"one": "{count}", "other": "{count}"}}, "fa: usage.files needs"),
        ({"language__direction": "down"}, "fa: direction must be ltr or rtl"),
    ],
)
def test_the_checker_finds(changes: dict[str, object], problem: str) -> None:
    found = i18n.problems("fa", broken(**changes), REFERENCE)
    assert any(line.startswith(problem) for line in found), found


def test_a_language_needs_its_plural_rule_first() -> None:
    assert i18n.problems("xx", REFERENCE, REFERENCE) == [
        "xx: add its plural rule to PLURALS in i18n.py"
    ]


@pytest.mark.parametrize(
    ("code", "forms"),
    [
        ("en", {1: "one", 2: "other", 0: "other"}),
        ("fa", {1: "other", 5: "other"}),
        ("ru", {1: "one", 21: "one", 11: "many", 2: "few", 22: "few", 12: "many", 5: "many"}),
        ("ar", {0: "zero", 1: "one", 2: "two", 3: "few", 110: "few", 11: "many", 100: "other"}),
    ],
)
def test_plural_rules(code: str, forms: dict[int, str]) -> None:
    rule, _ = i18n.PLURALS[code]
    assert {n: rule(n) for n in forms} == forms


# ------------------------------------------------------------ direction


def test_every_line_starts_in_the_readers_direction() -> None:
    for t, mark in ((EN, LRM), (FA, RLM)):
        text = t.text("menu.rename_prompt", name="x")
        assert all(line.startswith(mark) for line in text.splitlines())


def test_empty_lines_stay_empty() -> None:
    assert EN.lines(Html("a\n\nb")) == f"{LRM}a\n\n{LRM}b"


def test_other_peoples_words_are_escaped_and_isolated() -> None:
    text = FA.text("menu.new_name", name="<i>film</i>.mkv")
    assert f"{FSI}&lt;i&gt;film&lt;/i&gt;.mkv{PDI}" in text


def test_the_bots_own_html_is_not_isolated() -> None:
    assert FSI not in EN.text("file.over_limit", reason=Html("<b>no</b>"))


def test_labels_carry_no_marks() -> None:
    for t in (EN, FA):
        label = t.label("admin.cache", state=t.word("admin.on"))
        assert not set(label) & {LRM, RLM, FSI, PDI}
    assert FA.label("admin.cache", state=FA.word("admin.on")) == "🗄 کش: روشن"


def test_pop_ups_are_plain_text() -> None:
    assert EN.alert("menu.expired_alert") == f"{LRM}This menu has expired. Send the file again."
    assert "<" not in EN.alert("join.prompt")


# ------------------------------------------------------------ numbers


def test_persian_numbers() -> None:
    assert FA.number(1234) == "۱۲۳۴"
    assert FA.size(1536 * 1024 * 1024) == "۱٫۵ گیگابایت"
    assert FA.percent(0.4) == "۴۰٪"
    assert FA.clock(3723) == "۱:۰۲:۰۳"
    assert FA.phrase("usage.files", count=3) == "۳ فایل"


def test_english_numbers() -> None:
    assert EN.size(512) == "512 B"
    assert EN.size(1536 * 1024) == "1.5 MB"
    assert EN.phrase("usage.files", count=1) == "1 file"


@pytest.mark.parametrize(
    ("seconds", "english", "persian"),
    [
        (30, "1 min", "۱ دقیقه"),
        (90, "2 min", "۲ دقیقه"),
        (9000, "2 h 30 min", "۲ ساعت و ۳۰ دقیقه"),
        (7200, "2 h", "۲ ساعت"),
        (2 * 86400 + 3 * 3600 + 60, "2 days 4 h", "۲ روز و ۴ ساعت"),
    ],
)
def test_spans_round_up(seconds: int, english: str, persian: str) -> None:
    assert (EN.span(seconds), FA.span(seconds)) == (english, persian)


def test_a_plural_message_needs_a_count() -> None:
    with pytest.raises(TypeError):
        EN.phrase("usage.files")


def test_a_bool_is_not_a_number() -> None:
    with pytest.raises(TypeError):
        EN.phrase("usage.files", count=True)


# ------------------------------------------------------------ choosing


def test_someone_who_has_not_chosen_gets_the_default() -> None:
    persian_first = i18n.load("fa")
    assert persian_first.get(None) is persian_first.get("fa")
    assert persian_first.get("gone") is persian_first.get("fa")
    assert [t.code for t in persian_first] == ["fa", "en"]


def test_a_missing_message_falls_back_to_english() -> None:
    data = broken(menu__cancel=None)
    partial = i18n.build("fa", data, EN)
    assert partial.label("menu.cancel") == "✖️ Cancel"


def test_a_broken_language_file_is_left_out(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    persian = broken(language__digits="123")
    monkeypatch.setattr(i18n, "read", lambda code: persian if code == "fa" else REFERENCE)
    locales = i18n.load()
    assert "fa" not in locales
    assert "digits must be the ten digits" in caplog.text


def test_a_file_with_gaps_still_loads(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    persian = broken(menu__cancel=None)
    monkeypatch.setattr(i18n, "read", lambda code: persian if code == "fa" else REFERENCE)
    locales = i18n.load()
    assert locales.get("fa").label("menu.cancel") == "✖️ Cancel"
    assert "fa: missing menu.cancel" in caplog.text
