"""Searchable terminal choices with keyboard navigation and a back action."""

from typing import TypeVar

from prompt_toolkit import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.styles import Style

T = TypeVar("T")


def choose(
    title: str, choices: list[tuple[T, str]], *, default: T | None = None
) -> T | None:
    """Return a choice, None on Escape, or raise KeyboardInterrupt on Ctrl+C."""
    if not choices:
        raise ValueError("No choices available.")
    selected = next((i for i, (value, _) in enumerate(choices) if value == default), 0)
    search = Buffer()
    bindings = KeyBindings()

    def matches() -> list[tuple[T, str]]:
        words = search.text.casefold().split()
        return [
            item
            for item in choices
            if all(word in item[1].casefold() for word in words)
        ]

    def reset_selection(_: Buffer) -> None:
        nonlocal selected
        selected = 0

    search.on_text_changed += reset_selection

    def render() -> list[tuple[str, str]]:
        items = matches()
        if not items:
            return [("", "  No matches. Change your search.\n")]
        start = max(0, selected - 7)
        lines = []
        for i in range(start, min(len(items), start + 10)):
            active = i == selected
            lines.append(
                (
                    "class:selected" if active else "",
                    f"{' >' if active else '  '} {items[i][1]}\n",
                )
            )
        lines.append(("class:hint", f"  {selected + 1}/{len(items)}\n"))
        return lines

    @bindings.add("up")
    @bindings.add("down")
    def move(event) -> None:
        nonlocal selected
        count = len(matches())
        if count:
            selected = (
                selected + (1 if event.key_sequence[0].key == "down" else -1)
            ) % count

    @bindings.add("enter")
    def accept(event) -> None:
        items = matches()
        if items:
            event.app.exit(result=items[selected][0])

    @bindings.add("escape", eager=True)
    def back(event) -> None:
        event.app.exit(result=None)

    @bindings.add("c-c")
    @bindings.add("c-d")
    def cancel(event) -> None:
        event.app.exit(exception=KeyboardInterrupt())

    layout = Layout(
        HSplit(
            [
                Window(FormattedTextControl(title), wrap_lines=True),
                Window(FormattedTextControl("Search: "), height=1),
                Window(BufferControl(search), height=1),
                Window(
                    FormattedTextControl(render),
                    height=Dimension(
                        min=2, preferred=min(len(choices) + 1, 11), max=11
                    ),
                    wrap_lines=False,
                ),
                Window(
                    FormattedTextControl(
                        "Up/Down: select | Type: search | Enter: confirm | Esc: back | Ctrl+C: exit"
                    ),
                    height=2,
                    wrap_lines=True,
                ),
            ]
        )
    )
    return Application(
        layout=layout,
        key_bindings=bindings,
        style=Style.from_dict({"selected": "bold ansicyan", "hint": "ansibrightblack"}),
        full_screen=False,
        erase_when_done=True,
    ).run()
