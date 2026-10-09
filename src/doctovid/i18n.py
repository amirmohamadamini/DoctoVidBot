"""Languages: the bot's words, and how to show them in either direction.

Each language is one TOML file in `locales/`, named by its code: `en.toml`,
`fa.toml`. English is the reference. Every other file must have the same keys,
the same {placeholders} and the same HTML tags; `problems` checks that, and
the tests run it over every file. Adding a language is adding a file, plus its
plural rule in `PLURALS` if it isn't there yet.

Right-to-left and left-to-right text share one chat, one message and often one
line: a Persian sentence about `holiday.mkv`, or an English one about
`فیلم.mp4`. Telegram picks each line's direction from its first letter, so

- every line of a message starts with an invisible mark in the reader's
  direction (RLM or LRM), and
- everything the bot didn't write, file names above all, is wrapped in an
  isolate (FSI…PDI), so it is laid out on its own and can't reorder the
  sentence around it.
"""

from __future__ import annotations

import html
import logging
import math
import re
import string
import tomllib
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from importlib import resources
from typing import Literal

log = logging.getLogger(__name__)

REFERENCE = "en"

LRM = "\u200e"
RLM = "\u200f"
FSI = "\u2068"
PDI = "\u2069"

Direction = Literal["ltr", "rtl"]


class Html(str):
    """Text that is already safe for Telegram's HTML mode. Inserted as it is,
    where a plain `str` is taken to be someone else's words and escaped."""


PluralRule = Callable[[int], str]


def _one_other(n: int) -> str:
    return "one" if n == 1 else "other"


def _zero_one_other(n: int) -> str:
    return "one" if n in (0, 1) else "other"


def _no_plural(n: int) -> str:
    return "other"


def _east_slavic(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "one"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "few"
    return "many"


def _arabic(n: int) -> str:
    if n in (0, 1, 2):
        return ("zero", "one", "two")[n]
    if 3 <= n % 100 <= 10:
        return "few"
    if 11 <= n % 100 <= 99:
        return "many"
    return "other"


#: How each language chooses the form of a word after a whole number, from the
#: CLDR plural rules, and the forms its files must give. "other" is also the
#: fallback, so it is always required.
PLURALS: dict[str, tuple[PluralRule, tuple[str, ...]]] = {
    "en": (_one_other, ("one", "other")),
    "de": (_one_other, ("one", "other")),
    "es": (_one_other, ("one", "other")),
    "fr": (_zero_one_other, ("one", "other")),
    # A noun after a number stays singular in Persian and Turkish: one form.
    "fa": (_no_plural, ("other",)),
    "tr": (_no_plural, ("other",)),
    "ru": (_east_slavic, ("one", "few", "many", "other")),
    "uk": (_east_slavic, ("one", "few", "many", "other")),
    "ar": (_arabic, ("zero", "one", "two", "few", "many", "other")),
}

PLURAL_FORMS = frozenset({"zero", "one", "two", "few", "many", "other"})

Message = str | Mapping[str, str]

_TAG = re.compile(r"<[^>]*>")


def plain(text: str) -> str:
    """HTML text as plain text, for the places Telegram shows no formatting:
    button pop-ups."""
    return html.unescape(_TAG.sub("", text))


@dataclass(frozen=True, eq=False)
class Locale:
    code: str
    name: str
    direction: Direction
    digits: str
    decimal: str
    messages: Mapping[str, Message]
    plural: PluralRule
    fallback: Locale | None = field(default=None, repr=False)

    @property
    def mark(self) -> str:
        return RLM if self.direction == "rtl" else LRM

    # ------------------------------------------------------------ messages

    def text(self, key: str, /, **values: object) -> Html:
        """A message, ready to send: every line anchored in this language's
        direction."""
        return self.lines(self.phrase(key, **values))

    def lines(self, *parts: Html) -> Html:
        """Rendered parts, one or more lines each, as a message: every line
        starts with a mark in this language's direction."""
        text = "\n".join(parts)
        return Html("\n".join(self.mark + line if line else line for line in text.split("\n")))

    def phrase(self, key: str, /, **values: object) -> Html:
        """A message to go inside another one, without the anchoring."""
        rendered = {name: self._value(value) for name, value in values.items()}
        return Html(self._template(key, values.get("count")).format_map(rendered))

    def label(self, key: str, /, **values: object) -> str:
        """A button's label: plain text, with no invisible marks. Telegram
        lays out a single line well by itself, and a keyboard button's label
        comes back to the bot as a message that has to match it exactly."""
        return plain(self.phrase(key, **values))

    def alert(self, key: str, /, **values: object) -> str:
        return plain(self.text(key, **values))

    def word(self, key: str) -> Html:
        """A short message quoted inside another, like a button's name in
        "tap ❓ Help": no marks, no isolation."""
        return Html(html.escape(self.label(key), quote=False))

    # ------------------------------------------------------------ numbers

    def number(self, value: float, decimals: int = 0) -> Html:
        text = f"{value:.{decimals}f}".replace(".", self.decimal)
        return Html(text.translate(str.maketrans("0123456789", self.digits)))

    def size(self, n: float) -> Html:
        for unit in ("b", "kb", "mb", "gb"):
            if n < 1024 or unit == "gb":
                value = self.number(n, 0 if unit == "b" else 1)
                return self.phrase("unit.size", value=value, unit=self.word(f"unit.{unit}"))
            n /= 1024
        raise AssertionError("unreachable")

    def speed(self, bytes_per_second: float) -> Html:
        return self.phrase("unit.speed", size=self.size(bytes_per_second))

    def percent(self, fraction: float) -> Html:
        return self.phrase("unit.percent", value=self.number(fraction * 100))

    def clock(self, seconds: float) -> Html:
        total = int(seconds)
        hours, rest = divmod(total, 3600)
        minutes, secs = divmod(rest, 60)
        text = f"{hours}:{minutes:02}:{secs:02}" if hours else f"{minutes}:{secs:02}"
        return Html(text.translate(str.maketrans("0123456789", self.digits)))

    def span(self, seconds: float) -> Html:
        """A wait, in its two largest units and rounded up: "2 h 30 min",
        "3 days 4 h". Never less than it really is."""
        if seconds >= 24 * 3600:
            days, hours = divmod(math.ceil(seconds / 3600), 24)
            units = (("days", days), ("hours", hours))
        else:
            hours, minutes = divmod(max(1, math.ceil(seconds / 60)), 60)
            units = (("hours", hours), ("minutes", minutes))
        parts = [self.phrase(f"unit.{unit}", count=count) for unit, count in units if count]
        if len(parts) == 1:
            return parts[0]
        return self.phrase("unit.pair", first=parts[0], second=parts[1])

    # ------------------------------------------------------------ internals

    def _template(self, key: str, count: object) -> str:
        message = self.messages.get(key)
        if message is None:
            if self.fallback is None:
                raise KeyError(f"no message {key!r}")
            return self.fallback._template(key, count)
        if isinstance(message, str):
            return message
        if not isinstance(count, int):
            raise TypeError(f"{key!r} has plural forms, so it needs a whole-number count")
        return message.get(self.plural(count)) or message["other"]

    def _value(self, value: object) -> str:
        if isinstance(value, Html):
            return value
        if isinstance(value, bool):
            raise TypeError("say it in words: a bool has no translation")
        if isinstance(value, int):
            return self.number(value)
        if isinstance(value, str):
            # Someone else's words: escaped, and laid out on their own.
            return FSI + html.escape(value, quote=False) + PDI
        raise TypeError(f"can't put a {type(value).__name__} in a message")


class Locales:
    """Every language the bot speaks, the default first."""

    def __init__(self, locales: Mapping[str, Locale], default: str) -> None:
        if default not in locales:
            raise ValueError(f"no language {default!r}")
        self.default = locales[default]
        self._all = {default: self.default, **locales}

    def get(self, code: str | None) -> Locale:
        """The language `code`, or the default for someone who hasn't chosen,
        or chose one that has since been removed."""
        return self._all.get(code or "", self.default)

    def __iter__(self) -> Iterator[Locale]:
        return iter(self._all.values())

    def __contains__(self, code: object) -> bool:
        return code in self._all


# ------------------------------------------------------------ loading


def available() -> list[str]:
    """The codes of the languages shipped with the bot."""
    return sorted(
        entry.name.removesuffix(".toml")
        for entry in resources.files(__package__).joinpath("locales").iterdir()
        if entry.name.endswith(".toml")
    )


def read(code: str) -> dict[str, object]:
    source = resources.files(__package__).joinpath("locales", f"{code}.toml")
    return tomllib.loads(source.read_text(encoding="utf-8"))


def build(code: str, data: Mapping[str, object], fallback: Locale | None) -> Locale:
    about = data["language"]
    assert isinstance(about, dict)
    rule, _ = PLURALS[code]
    return Locale(
        code=code,
        name=str(about["name"]),
        direction="rtl" if about["direction"] == "rtl" else "ltr",
        digits=str(about["digits"]),
        decimal=str(about["decimal"]),
        messages=flatten(data),
        plural=rule,
        fallback=fallback,
    )


def load(default: str = REFERENCE) -> Locales:
    """Every language file. One with mistakes still loads, with its gaps
    filled in English, and the mistakes are logged; the tests fail on them."""
    english = read(REFERENCE)
    reference = build(REFERENCE, english, None)
    locales = {REFERENCE: reference}
    for code in available():
        if code == REFERENCE:
            continue
        data = read(code)
        if unusable := _about_problems(code, data):
            log.error("language file left out: %s", "; ".join(unusable))
            continue
        for problem in problems(code, data, english):
            log.warning("language file %s", problem)
        locales[code] = build(code, data, reference)
    return Locales(locales, default)


def flatten(data: Mapping[str, object], prefix: str = "") -> dict[str, Message]:
    """TOML tables into dotted keys. A table whose keys are all plural forms
    is one message with plural forms, not a section."""
    flat: dict[str, Message] = {}
    for key, value in data.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict) and value and set(value) <= PLURAL_FORMS:
            flat[name] = {form: str(text) for form, text in value.items()}
        elif isinstance(value, dict):
            flat.update(flatten(value, f"{name}."))
        else:
            flat[name] = str(value)
    return flat


# ------------------------------------------------------------ checking


def problems(code: str, data: Mapping[str, object], reference: Mapping[str, object]) -> list[str]:
    """Everything wrong with a language file, compared with English."""
    found = _about_problems(code, data)
    if code not in PLURALS:
        return found

    ours, theirs = flatten(data), flatten(reference)
    found += [f"{code}: missing {key}" for key in theirs.keys() - ours.keys()]
    found += [f"{code}: {key} isn't used" for key in ours.keys() - theirs.keys()]
    _, forms = PLURALS[code]
    for key in sorted(ours.keys() & theirs.keys()):
        mine, english = ours[key], theirs[key]
        if isinstance(english, str) != isinstance(mine, str):
            found.append(
                f"{code}: {key} must {'not ' if isinstance(english, str) else ''}have plural forms"
            )
            continue
        if not isinstance(mine, str) and set(mine) != set(forms):
            found.append(f"{code}: {key} needs exactly the forms {', '.join(forms)}")
        if _fields(mine) != _fields(english):
            found.append(f"{code}: {key} must use {sorted(_fields(english))}")
        if _tags(mine) != _tags(english):
            found.append(f"{code}: {key} must use the tags {_tags(english)}")
    return found


def _about_problems(code: str, data: Mapping[str, object]) -> list[str]:
    """Mistakes that stop a language from loading at all."""
    if code not in PLURALS:
        return [f"{code}: add its plural rule to PLURALS in i18n.py"]
    about = data.get("language")
    if not isinstance(about, dict):
        return [f"{code}: the [language] table is missing"]
    found = []
    if not isinstance(about.get("name"), str):
        found.append(f"{code}: name must be the language's name, in the language")
    if about.get("direction") not in ("ltr", "rtl"):
        found.append(f"{code}: direction must be ltr or rtl")
    digits = about.get("digits")
    if not isinstance(digits, str) or len(digits) != 10:
        found.append(f"{code}: digits must be the ten digits, 0 to 9")
    decimal = about.get("decimal")
    if not isinstance(decimal, str) or len(decimal) != 1:
        found.append(f"{code}: decimal must be one character")
    return found


def _texts(message: Message) -> list[str]:
    return [message] if isinstance(message, str) else list(message.values())


def _fields(message: Message) -> set[str]:
    return {
        name for text in _texts(message) for _, name, _, _ in string.Formatter().parse(text) if name
    }


def _tags(message: Message) -> list[str]:
    return sorted(tag for text in _texts(message)[:1] for tag in _TAG.findall(text))
