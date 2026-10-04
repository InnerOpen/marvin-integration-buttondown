"""Relative links in an issue body become absolute against the site URL; everything else is left alone."""

import pytest

from marvin_integration_buttondown.links import absolutize_links, has_relative_links, normalise_base_url

BASE = "https://example.com/"


def test_absolutize_links_rewrites_root_relative_markdown_links():
    assert absolutize_links("See [a work](/works/blue).", BASE) == "See [a work](https://example.com/works/blue)."


def test_absolutize_links_rewrites_markdown_images_and_titles():
    body = '![Blue](/img/blue.jpg "Blue") and [x](works/x "t")'
    assert absolutize_links(body, BASE) == '![Blue](https://example.com/img/blue.jpg "Blue") and [x](https://example.com/works/x "t")'


def test_absolutize_links_rewrites_angle_bracket_and_reference_links():
    body = "[a](</works/a>)\n\n[ref]: /works/ref\n  [two]: <works/two>"
    assert (
        absolutize_links(body, BASE)
        == "[a](<https://example.com/works/a>)\n\n[ref]: https://example.com/works/ref\n  [two]: <https://example.com/works/two>"
    )


def test_absolutize_links_rewrites_html_href_and_src():
    body = "<a href=\"/works/x\">x</a> <img src='/img/y.png'>"
    assert absolutize_links(body, BASE) == "<a href=\"https://example.com/works/x\">x</a> <img src='https://example.com/img/y.png'>"


@pytest.mark.parametrize(
    "body",
    [
        "[x](https://other.example/a)",
        "[x](http://other.example/a)",
        "[mail](mailto:hi@example.com)",
        "[call](tel:+15550100)",
        "[cdn](//cdn.example/a.js)",
        "[top](#top)",
        '<a href="https://other.example/a">a</a>',
        "[unsubscribe]({{ unsubscribe_url }})",
    ],
)
def test_absolutize_links_leaves_absolute_and_special_links_untouched(body):
    assert absolutize_links(body, BASE) == body


def test_absolutize_links_without_a_base_url_changes_nothing():
    assert absolutize_links("[a](/works/a)", "") == "[a](/works/a)"


def test_absolutize_links_keeps_a_base_path():
    assert absolutize_links("[a](/works/a)", "https://example.com/site/") == "[a](https://example.com/site/works/a)"


def test_has_relative_links_detects_only_relative_ones():
    assert has_relative_links("[a](/works/a)") and has_relative_links('<a href="x">')
    assert not has_relative_links("[a](https://example.com/a) [b](#b)")


def test_normalise_base_url_adds_the_trailing_slash_and_rejects_non_http():
    assert normalise_base_url(" https://example.com ") == BASE
    assert normalise_base_url("") == ""
    with pytest.raises(ValueError, match="http"):
        normalise_base_url("example.com")
