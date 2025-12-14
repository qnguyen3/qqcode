from __future__ import annotations

from datetime import datetime
import random
from time import monotonic
from typing import ClassVar

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Static


class LoadingWidget(Static):
    BRAILLE_SPINNER = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

    # Small, frequent updates + time-based phase produces a smoother gradient animation.
    UPDATE_INTERVAL_SECONDS = 0.05

    # Keep roughly the same speed as before:
    # - previous: 1 palette step per 0.1s -> 10 steps/s -> 0.5s per full 5-color cycle
    GRADIENT_CYCLE_SECONDS = 0.5
    SPINNER_FPS = 10.0

    TARGET_COLORS = ("#ff0090", "#ff4db4", "#cc66cc", "#669aee", "#00ceff")

    EASTER_EGGS: ClassVar[list[str]] = [
        "Thinking deeply",
        "Compiling thoughts",
        "Optimizing ideas",
        "Aligning neurons",
        "Simulating futures",
        "Spinning vectors",
        "Booting cognition",
        "Reading Proust",
        "Counting Rs",
        "Coding quietly",
        "Vibing softly",
        "Reorganizing world",
    ]

    EASTER_EGGS_HALLOWEEN: ClassVar[list[str]] = [
        "Trick or treating",
        "Carving pumpkins",
        "Summoning spirits",
        "Brewing potions",
        "Haunting the terminal",
        "Petting le chat noir",
    ]

    EASTER_EGGS_DECEMBER: ClassVar[list[str]] = [
        "Wrapping presents",
        "Decorating trees",
        "Drinking cocoa",
        "Building snowmen",
        "Writing cards",
    ]

    def __init__(self, status: str | None = None) -> None:
        super().__init__(classes="loading-widget")
        self.status = status or self._get_default_status()
        self.spinner_pos = 0
        self.char_widgets: list[Static] = []
        self.spinner_widget: Static | None = None
        self.ellipsis_widget: Static | None = None
        self.hint_widget: Static | None = None
        self.start_time: float | None = None

    def _get_easter_egg(self) -> str | None:
        EASTER_EGG_PROBABILITY = 0.10
        if random.random() < EASTER_EGG_PROBABILITY:
            available_eggs = list(self.EASTER_EGGS)

            OCTOBER = 10
            HALLOWEEN_DAY = 31
            DECEMBER = 12
            now = datetime.now()
            if now.month == OCTOBER and now.day == HALLOWEEN_DAY:
                available_eggs.extend(self.EASTER_EGGS_HALLOWEEN)
            if now.month == DECEMBER:
                available_eggs.extend(self.EASTER_EGGS_DECEMBER)

            return random.choice(available_eggs)
        return None

    def _get_default_status(self) -> str:
        return self._get_easter_egg() or "Thinking"

    def _apply_easter_egg(self, status: str) -> str:
        return self._get_easter_egg() or status

    def set_status(self, status: str) -> None:
        self.status = self._apply_easter_egg(status)
        self._rebuild_chars()

    def compose(self) -> ComposeResult:
        with Horizontal(classes="loading-container"):
            self.spinner_widget = Static(
                self.BRAILLE_SPINNER[0] + " ", classes="loading-star"
            )
            yield self.spinner_widget

            with Horizontal(classes="loading-status"):
                for char in self.status:
                    widget = Static(char, classes="loading-char")
                    self.char_widgets.append(widget)
                    yield widget

            self.ellipsis_widget = Static("… ", classes="loading-ellipsis")
            yield self.ellipsis_widget

            self.hint_widget = Static("(0s esc to interrupt)", classes="loading-hint")
            yield self.hint_widget

    def _rebuild_chars(self) -> None:
        if not self.is_mounted:
            return

        status_container = self.query_one(".loading-status", Horizontal)

        status_container.remove_children()
        self.char_widgets.clear()

        for char in self.status:
            widget = Static(char, classes="loading-char")
            self.char_widgets.append(widget)
            status_container.mount(widget)

        self.update_animation()

    def on_mount(self) -> None:
        self.start_time = monotonic()
        self.update_animation()
        self.set_interval(self.UPDATE_INTERVAL_SECONDS, self.update_animation)

    def _lerp_color(self, color_a: str, color_b: str, t: float) -> str:
        """Blend two #RRGGBB colors."""

        def to_rgb(hex_color: str) -> tuple[int, int, int]:
            hex_color = hex_color.removeprefix("#")
            return (
                int(hex_color[0:2], 16),
                int(hex_color[2:4], 16),
                int(hex_color[4:6], 16),
            )

        def clamp(value: float, low: float, high: float) -> float:
            return max(low, min(high, value))

        t = clamp(t, 0.0, 1.0)
        r0, g0, b0 = to_rgb(color_a)
        r1, g1, b1 = to_rgb(color_b)

        r = round(r0 + (r1 - r0) * t)
        g = round(g0 + (g1 - g0) * t)
        b = round(b0 + (b1 - b0) * t)
        return f"#{r:02x}{g:02x}{b:02x}"

    def _get_gradient_color(self, position: int, phase: float) -> str:
        palette_len = len(self.TARGET_COLORS)
        palette_pos = (position - phase) % palette_len

        idx0 = int(palette_pos)
        idx1 = (idx0 + 1) % palette_len
        t = palette_pos - idx0

        return self._lerp_color(self.TARGET_COLORS[idx0], self.TARGET_COLORS[idx1], t)

    def update_animation(self) -> None:
        elapsed = 0.0
        if self.start_time is not None:
            elapsed = monotonic() - self.start_time

        gradient_speed = len(self.TARGET_COLORS) / self.GRADIENT_CYCLE_SECONDS
        gradient_phase = elapsed * gradient_speed

        if self.spinner_widget:
            self.spinner_pos = int(elapsed * self.SPINNER_FPS) % len(
                self.BRAILLE_SPINNER
            )
            spinner_char = self.BRAILLE_SPINNER[self.spinner_pos]
            color_0 = self._get_gradient_color(0, gradient_phase)
            color_1 = self._get_gradient_color(1, gradient_phase)
            self.spinner_widget.update(f"[{color_0}]{spinner_char}[/][{color_1}] [/]")

        for i, widget in enumerate(self.char_widgets):
            position = 2 + i
            color = self._get_gradient_color(position, gradient_phase)
            widget.update(f"[{color}]{self.status[i]}[/]")

        if self.ellipsis_widget:
            ellipsis_start = 2 + len(self.status)
            color_ellipsis = self._get_gradient_color(ellipsis_start, gradient_phase)
            color_space = self._get_gradient_color(ellipsis_start + 1, gradient_phase)
            self.ellipsis_widget.update(f"[{color_ellipsis}]…[/][{color_space}] [/]")

        if self.hint_widget and self.start_time is not None:
            self.hint_widget.update(f"({int(elapsed)}s esc to interrupt)")
