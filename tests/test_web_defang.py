"""URL defanging and snippet escaping.

Two related guarantees about archived content reaching the browser:

1. No URL in a message can cause a fetch. The CSP and the sandboxed iframe
   already prevent it; defanging means the markup is inert even after it
   leaves this app.
2. Message text is never treated as markup. `ts_headline` output is derived
   from the message body, so rendering it unescaped is an injection.
"""

from __future__ import annotations

import pytest

from gmail_archive.web.app import templates
from gmail_archive.web.filters import defang


class TestDefang:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("http://tracker.example/p.gif", "hxxp://tracker.example/p.gif"),
            ("https://tracker.example/p.gif", "hxxps://tracker.example/p.gif"),
            ("HTTP://SHOUTY.EXAMPLE", "hxxp://SHOUTY.EXAMPLE"),
            ("HtTpS://Mixed.example", "hxxps://Mixed.example"),
            ("ftp://files.example/x", "fxp://files.example/x"),
            ("ws://socket.example", "wxs://socket.example"),
            ("file:///etc/passwd", "fxle:///etc/passwd"),
        ],
    )
    def test_schemes_are_defanged(self, raw: str, expected: str) -> None:
        assert defang(raw) == expected

    def test_protocol_relative_urls_are_caught(self) -> None:
        # These inherit the page's scheme and fetch perfectly well.
        assert '<img src="//t.example/p.gif">' not in defang(
            '<img src="//t.example/p.gif">'
        )
        assert "hxxp://t.example" in defang('<img src="//t.example/p.gif">')

    def test_bare_double_slash_in_text_is_left_alone(self) -> None:
        # Only attributes are rewritten, so prose and code survive intact.
        assert defang("see the // comment") == "see the // comment"

    def test_mailto_and_data_are_untouched(self) -> None:
        # mailto fetches nothing; data: is inline by definition, and defanging
        # it would break embedded images that never touch the network.
        assert defang("mailto:a@example.com") == "mailto:a@example.com"
        assert defang("data:image/png;base64,AAAA") == "data:image/png;base64,AAAA"

    def test_urls_stay_readable(self) -> None:
        # The point is inert, not destroyed: an archive should still show you
        # where a link pointed.
        out = defang("https://bank.example/login?token=abc")
        assert "bank.example/login?token=abc" in out

    def test_none_and_empty(self) -> None:
        assert defang(None) == ""
        assert defang("") == ""

    def test_html_attributes_are_defanged(self) -> None:
        out = defang('<a href="http://x.example">click</a><img src="https://y/p">')
        assert "http://" not in out
        assert "https://" not in out
        assert 'href="hxxp://x.example"' in out


class TestSnippetEscaping:
    """The search snippet is message text; only the query's own markers may
    become markup.

    Regression test. The previous template piped the ts_headline snippet
    through `|safe` with no `escape()` first, so a message whose body contained
    markup injected it into the results page. 274 messages in the reference
    archive carry `<script` or `onerror=` in their searchable text.
    """

    def _render(self, snippet: str) -> str:
        template = templates.env.get_template("_row.html")
        row = type(
            "Row",
            (),
            {
                "raw_sha256": "0" * 64,
                "subject": "s",
                "from_addr": "a@example.com",
                "internal_date": None,
                "snippet": snippet,
                "is_unread": False,
                "is_starred": False,
                "is_important": False,
                "user_labels": [],
            },
        )()
        return template.render(msg=row, highlight=True)

    def test_script_in_a_snippet_is_escaped(self) -> None:
        out = self._render("hello <script>alert(1)</script> world")
        assert "<script>" not in out
        assert "&lt;script&gt;" in out

    def test_event_handler_markup_is_escaped(self) -> None:
        out = self._render("<img src=x onerror=alert(1)>")
        assert "<img" not in out
        assert "&lt;img" in out

    def test_the_query_highlight_markers_still_become_markup(self) -> None:
        # Escaping must not defeat the feature: [hl] is inserted by our own
        # query, not by the message, so it is the one thing allowed through.
        out = self._render("an [hl]invoice[/hl] arrived")
        assert "<mark>invoice</mark>" in out

    def test_a_message_cannot_forge_a_highlight(self) -> None:
        # A body containing a literal <mark> gets escaped like anything else.
        out = self._render("<mark>not really highlighted</mark>")
        assert "&lt;mark&gt;" in out

    def test_urls_in_snippets_are_defanged(self) -> None:
        out = self._render("click http://tracker.example/x now")
        assert "http://" not in out
        assert "hxxp://tracker.example/x" in out
