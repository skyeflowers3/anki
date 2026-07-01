# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
from __future__ import annotations

import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import aqt
import aqt.forms
import aqt.main
from anki.decks import DeckId
from anki.utils import is_mac
from aqt import gui_hooks
from aqt.operations.deck import set_current_deck
from aqt.qt import *
from aqt.theme import theme_manager
from aqt.utils import (
    disable_help_button,
    getSaveFile,
    maybeHideClose,
    restoreGeom,
    saveGeom,
    tooltip,
    tr,
)
from aqt.webview import AnkiWebView, AnkiWebViewKind, LegacyStatsWebView


def _memory_score_module():
    """Import the Speedrun MCAT memory-score module.

    The reusable logic lives in speedrun/memory_score.py at the repo root so it
    can be shared with the standalone CLI. When running from source that folder
    is not on sys.path by default, so add it relative to the aqt package.
    """
    try:
        from speedrun import memory_score
    except ModuleNotFoundError:
        repo_root = Path(aqt.__file__).resolve().parents[2]
        if str(repo_root) not in sys.path:
            sys.path.insert(0, str(repo_root))
        from speedrun import memory_score

    return memory_score


def _performance_score_module():
    """Import the Speedrun MCAT performance-score module.

    Mirrors `_memory_score_module()`: the reusable logic lives in
    speedrun/performance_score.py at the repo root so it can be shared with the
    standalone CLI. Add that folder to sys.path relative to the aqt package when
    running from source.
    """
    try:
        from speedrun import performance_score
    except ModuleNotFoundError:
        repo_root = Path(aqt.__file__).resolve().parents[2]
        if str(repo_root) not in sys.path:
            sys.path.insert(0, str(repo_root))
        from speedrun import performance_score

    return performance_score


class NewDeckStats(QDialog):
    """New deck stats."""

    def __init__(self, mw: aqt.main.AnkiQt) -> None:
        QDialog.__init__(self, mw, Qt.WindowType.Window)
        mw.garbage_collect_on_dialog_finish(self)
        self.mw = mw
        self.name = "deckStats"
        self.period = 0
        self.form = aqt.forms.stats.Ui_Dialog()
        self.oldPos = None
        self.wholeCollection = False
        self.setMinimumWidth(700)
        disable_help_button(self)
        f = self.form
        f.setupUi(self)
        f.groupBox.setVisible(False)
        f.groupBox_2.setVisible(False)
        if not is_mac:
            f.horizontalLayout_4.setContentsMargins(0, 0, 0, 0)
        self._build_tabs()
        restoreGeom(self, self.name, default_size=(800, 800))

        from aqt.deckchooser import DeckChooser

        self.deck_chooser = DeckChooser(
            self.mw,
            f.deckArea,
            on_deck_changed=self.on_deck_changed,
            dyn=True,  # include filtered decks
        )

        b = f.buttonBox.addButton(
            tr.statistics_save_pdf(), QDialogButtonBox.ButtonRole.ActionRole
        )
        assert b is not None
        qconnect(b.clicked, self.saveImage)
        b.setAutoDefault(False)
        b = f.buttonBox.button(QDialogButtonBox.StandardButton.Close)
        assert b is not None
        b.setAutoDefault(False)
        maybeHideClose(self.form.buttonBox)
        gui_hooks.stats_dialog_will_show(self)
        self.form.web.hide_while_preserving_layout()
        self.show()
        self.refresh()
        self.form.web.set_bridge_command(self._on_bridge_cmd, self)
        self.activateWindow()

    def _build_tabs(self) -> None:
        """Host the existing graphs web view and an MCAT memory panel in tabs."""
        f = self.form
        layout = f.verticalLayout
        # detach the graphs web view from the top of the dialog layout
        layout.takeAt(0)

        self.tabs = QTabWidget(self)

        graphs_tab = QWidget()
        graphs_layout = QVBoxLayout(graphs_tab)
        graphs_layout.setContentsMargins(0, 0, 0, 0)
        graphs_layout.addWidget(f.web)
        self.tabs.addTab(graphs_tab, "Graphs")

        self.memory_web = AnkiWebView(kind=AnkiWebViewKind.MCAT_MEMORY)
        self.memory_web.set_bridge_command(self._on_bridge_cmd, self)
        memory_tab = QWidget()
        memory_layout = QVBoxLayout(memory_tab)
        memory_layout.setContentsMargins(0, 0, 0, 0)
        memory_layout.addWidget(self.memory_web)
        self.tabs.addTab(memory_tab, "MCAT Memory")

        self.performance_web = AnkiWebView(kind=AnkiWebViewKind.MCAT_PERFORMANCE)
        self.performance_web.set_bridge_command(self._on_bridge_cmd, self)
        performance_tab = QWidget()
        performance_layout = QVBoxLayout(performance_tab)
        performance_layout.setContentsMargins(0, 0, 0, 0)
        performance_layout.addWidget(self.performance_web)
        self.tabs.addTab(performance_tab, "MCAT Performance")

        layout.insertWidget(0, self.tabs, stretch=1)

    def reject(self) -> None:
        self.deck_chooser.cleanup()
        self.form.web.cleanup()
        self.form.web = None  # type: ignore
        if getattr(self, "memory_web", None):
            self.memory_web.cleanup()
            self.memory_web = None  # type: ignore
        if getattr(self, "performance_web", None):
            self.performance_web.cleanup()
            self.performance_web = None  # type: ignore
        saveGeom(self, self.name)
        aqt.dialogs.markClosed("NewDeckStats")
        QDialog.reject(self)

    def closeWithCallback(self, callback: Callable[[], None]) -> None:
        self.reject()
        callback()

    def on_deck_changed(self, deck_id: int) -> None:
        set_current_deck(parent=self, deck_id=DeckId(deck_id)).success(
            lambda _: self.refresh()
        ).run_in_background()

    def _imagePath(self) -> str | None:
        name = time.strftime("-%Y-%m-%d@%H-%M-%S.pdf", time.localtime(time.time()))
        name = f"anki-{tr.statistics_stats()}{name}"
        file = getSaveFile(
            self,
            title=tr.statistics_save_pdf(),
            dir_description="stats",
            key="stats",
            ext=".pdf",
            fname=name,
        )
        return file

    def saveImage(self) -> None:
        path = self._imagePath()
        if not path:
            return

        # When scrolled down in dark mode, the top of the page in the
        # final PDF will have a white background, making the text and graphs
        # unreadable. A simple fix for now is to scroll to the top of the
        # page first.
        def after_scroll(arg: Any) -> None:
            form_web_page = self.form.web.page()
            assert form_web_page is not None
            form_web_page.printToPdf(path)
            tooltip(tr.statistics_saved())

        self.form.web.evalWithCallback("window.scrollTo(0, 0);", after_scroll)

    # legacy add-ons
    def changePeriod(self, n: Any) -> None:
        pass

    def changeScope(self, type: Any) -> None:
        pass

    def _on_bridge_cmd(self, cmd: str) -> bool:
        if cmd.startswith("browserSearch"):
            _, query = cmd.split(":", 1)
            browser = aqt.dialogs.open("Browser", self.mw)
            browser.search_for(query)

        return False

    def refresh(self) -> None:
        self.form.web.load_sveltekit_page("graphs")
        self._refresh_memory()
        self._refresh_performance()

    def _refresh_memory(self) -> None:
        memory_web = getattr(self, "memory_web", None)
        if memory_web is None:
            return
        try:
            memory_score = _memory_score_module()
            sections = memory_score.compute_sections(self.mw.col)
            body = memory_score.render_html(sections)
        except Exception as exc:
            body = (
                "<div style='padding:20px'>"
                f"Unable to compute the MCAT memory score: {exc}"
                "</div>"
            )
        memory_web.stdHtml(body, context=self)

    def _refresh_performance(self) -> None:
        performance_web = getattr(self, "performance_web", None)
        if performance_web is None:
            return
        try:
            performance_score = _performance_score_module()
            sections = performance_score.compute_sections(self.mw.col)
            body = performance_score.render_html(sections)
        except Exception as exc:
            body = (
                "<div style='padding:20px'>"
                f"Unable to compute the MCAT performance score: {exc}"
                "</div>"
            )
        performance_web.stdHtml(body, context=self)


class DeckStats(QDialog):
    """Legacy deck stats, used by some add-ons."""

    def __init__(self, mw: aqt.main.AnkiQt) -> None:
        QDialog.__init__(self, mw, Qt.WindowType.Window)
        mw.garbage_collect_on_dialog_finish(self)
        self.mw = mw
        self.name = "deckStats"
        self.period = 0
        self.form = aqt.forms.stats.Ui_Dialog()
        # Hack: Switch out web views dynamically to avoid maintaining multiple
        # Qt forms for different versions of the stats dialog.
        self.form.web = LegacyStatsWebView(self.mw)
        self.oldPos = None
        self.wholeCollection = False
        self.setMinimumWidth(700)
        disable_help_button(self)
        f = self.form
        if theme_manager.night_mode and not theme_manager.macos_dark_mode():
            # the grouping box renders incorrectly in the fusion theme. 5.9+
            # 5.13 behave differently to 5.14, but it looks bad in either case,
            # and adjusting the top margin makes the 'save PDF' button show in
            # the wrong place, so for now we just disable the border instead
            self.setStyleSheet("QGroupBox { border: 0; }")
        f.setupUi(self)
        restoreGeom(self, self.name)
        b = f.buttonBox.addButton(
            tr.statistics_save_pdf(), QDialogButtonBox.ButtonRole.ActionRole
        )
        assert b is not None
        qconnect(b.clicked, self.saveImage)
        b.setAutoDefault(False)
        qconnect(f.groups.clicked, lambda: self.changeScope("deck"))
        f.groups.setShortcut("g")
        qconnect(f.all.clicked, lambda: self.changeScope("collection"))
        qconnect(f.month.clicked, lambda: self.changePeriod(0))
        qconnect(f.year.clicked, lambda: self.changePeriod(1))
        qconnect(f.life.clicked, lambda: self.changePeriod(2))
        maybeHideClose(self.form.buttonBox)
        gui_hooks.stats_dialog_old_will_show(self)
        self.show()
        self.refresh()
        self.activateWindow()

    def reject(self) -> None:
        self.form.web.cleanup()
        self.form.web = None  # type: ignore
        saveGeom(self, self.name)
        aqt.dialogs.markClosed("DeckStats")
        QDialog.reject(self)

    def closeWithCallback(self, callback: Callable[[], None]) -> None:
        self.reject()
        callback()

    def _imagePath(self) -> str | None:
        name = time.strftime("-%Y-%m-%d@%H-%M-%S.pdf", time.localtime(time.time()))
        name = f"anki-{tr.statistics_stats()}{name}"
        file = getSaveFile(
            self,
            title=tr.statistics_save_pdf(),
            dir_description="stats",
            key="stats",
            ext=".pdf",
            fname=name,
        )
        return file

    def saveImage(self) -> None:
        path = self._imagePath()
        if not path:
            return
        form_web_page = self.form.web.page()
        assert form_web_page is not None
        form_web_page.printToPdf(path)
        tooltip(tr.statistics_saved())

    def changePeriod(self, n: int) -> None:
        self.period = n
        self.refresh()

    def changeScope(self, type: str) -> None:
        self.wholeCollection = type == "collection"
        self.refresh()

    def refresh(self) -> None:
        self.mw.progress.start(parent=self)
        stats = self.mw.col.stats()
        stats.wholeCollection = self.wholeCollection
        self.report = stats.report(type=self.period)
        self.form.web.stdHtml(
            f"<html><body>{self.report}</body></html>",
            js=["js/vendor/jquery.min.js", "js/vendor/plot.js"],
            context=self,
        )
        self.mw.progress.finish()
