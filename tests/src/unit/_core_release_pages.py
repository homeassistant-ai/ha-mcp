"""What GitHub and the Home Assistant blog serve about Core's releases.

The markup is the 2026.11 release post's, as the blog served it: its tags,
attributes and classes, the ``name`` anchor of the section heading, the
``details``/``summary``/``span`` nesting of each entry, the glossary tooltips
and the ``/integrations/<domain>/`` links. The release bodies keep GitHub's
shapes: pull-request lines with docs labels, a tagged breaking change, and a
monthly release that only links its post. Every sentence, name, person and
pull request in them is made up.
"""

from __future__ import annotations

from typing import Any

RELEASES = "https://api.github.com/repos/home-assistant/core/releases"
BLOG_202611 = "https://www.home-assistant.io/blog/2026/11/04/release-202611/"

_ARROW = """      <div class="details-block-arrow">
      <svg class="down" width="24" height="24" viewbox="0 0 24 24"><path d="M7.41,8.58L12,13.17L16.59,8.58L18,10L12,16L6,10L7.41,8.58Z"></path></svg>
      <svg class="up" width="24" height="24" viewbox="0 0 24 24"><path d="M7.41,15.41L12,10.83L16.59,15.41L18,14L12,8L6,14L7.41,15.41Z"></path></svg>
      </div>"""

# The section heading, its introduction, an entry with one integration link,
# one with none and glossary tooltips, one with two links, and the heading after
# the section.
BREAKING_CHANGES_202611 = f"""<h2>Backward-incompatible changes <a class="title-link" name="backward-incompatible-changes" href="#backward-incompatible-changes"></a>
</h2>
<p>Placeholder introduction to the section.</p>
<p>A second placeholder paragraph before the entries:</p>
<div class="details-block">
  <details class="details-block-item">
    <summary class="details-block-title">
      <span>Example Hub</span>
{_ARROW}
    </summary>
    <div class="details-block-content" id="content_0">
<p>The <strong>Sample</strong> switch of the Example Hub integration now needs a newer hub.</p>
<p>A second placeholder paragraph about the hub.</p>
<p>(<a rel="external nofollow" target="_blank" href="https://github.com/contributor-one">@contributor-one</a> - <a rel="external nofollow" target="_blank" href="https://github.com/home-assistant/core/pull/100001">#100001</a>) (<a href="/integrations/example_hub/">Example Hub documentation</a>)</p>
</div>
  </details>
</div>
<div class="details-block">
  <details class="details-block-item">
    <summary class="details-block-title">
      <span>Status card</span>
{_ARROW}
    </summary>
    <div class="details-block-content" id="content_1">
<p>The Status card now changes color only while the <span class="terminology">entity<span class="terminology-tooltip">Placeholder glossary definition of an entity.<a class="terminology-link" href="/docs/configuration/entities_domains/"> [Learn more]</a></span></span> is in a placeholder state. The <code>color</code> option is renamed to <code>status_color</code>.</p>
<p>If you wrote the card in <span class="terminology">YAML<span class="terminology-tooltip">Placeholder glossary definition of YAML, with <code>configuration.yaml</code> in it.<a class="terminology-link" href="/docs/configuration/yaml/"> [Learn more]</a></span></span> yourself, rename the option.</p>
<p>(<a rel="external nofollow" target="_blank" href="https://github.com/contributor-two">@contributor-two</a> - <a rel="external nofollow" target="_blank" href="https://github.com/home-assistant/frontend/pull/100002">frontend#100002</a>)</p>
</div>
  </details>
</div>
<div class="details-block">
  <details class="details-block-item">
    <summary class="details-block-title">
      <span>Calendar and Tide</span>
{_ARROW}
    </summary>
    <div class="details-block-content" id="content_7">
<p>Placeholder <span class="terminology">triggers<span class="terminology-tooltip">Placeholder glossary definition of a trigger.<a class="terminology-link" href="/docs/automation/trigger/"> [Learn more]</a></span></span> (<code>tide.*</code>) now read the sign of <code>offset</code>.</p>
<p>A second placeholder paragraph about these triggers.</p>
<p>(<a rel="external nofollow" target="_blank" href="https://github.com/contributor-three">@contributor-three</a> - <a rel="external nofollow" target="_blank" href="https://github.com/home-assistant/core/pull/100003">#100003</a>) (<a href="/integrations/calendar/">Calendar documentation</a>) (<a href="/integrations/tide/">Tide documentation</a>)</p>
</div>
  </details>
</div>
<h2>All changes <a class="title-link" name="all-changes" href="#all-changes"></a>
</h2>
<p>Placeholder text linking the <a href="/changelogs/core-2026.11">full changelog</a>.</p>
"""

# A patch body's lines, as GitHub's release bodies write them.
_BODIES = {
    "2026.11.2": (
        "- Bump placeholder-lib to v2.0.0 ([@contributor-four] - [#100010])"
        " ([example_hub docs]) (dependency)\n"
        "- Fix a placeholder options flow ([@contributor-five] - [#100011])"
        " ([example_net docs])"
    ),
    "2026.11.1": (
        "- Bump other-lib to v3 ([@contributor-four] - [#100012]) ([example_net docs])"
        " (dependency)\n"
        "- Fix placeholder events never arriving ([@contributor-six] -"
        " [#100013]) ([example_cam docs])"
    ),
    "2026.11.0": (
        "Placeholder release announcement.\n\n"
        "Read it in the [release notes]"
        "(https://www.home-assistant.io/blog/2026/11/04/release-202611/).\n"
    ),
    "2026.11.0b6": ("- Fix a placeholder translation (@contributor-seven - #100014)"),
    "2026.10.4": (
        "- Raise a placeholder limit ([@contributor-eight] - [#100015])\n"
        "- Bump third-lib to 0.30.0 ([@contributor-nine] - [#100016])"
        " ([example_bridge docs]) (dependency)"
    ),
    "2026.10.3": (
        "- Fix placeholder version detection ([@contributor-ten] - [#100017])"
        " ([example_vm docs])"
    ),
    "2026.10.2": (
        "- Fix a placeholder index error ([@contributor-one] -"
        " [#100018]) ([example_car docs])"
    ),
    "2026.10.1": (
        "- Split a placeholder range ([@contributor-two] - [#100019])"
        " ([example_mesh docs])\n"
        "- Remove the placeholder interval from `example_pool` ([@contributor-three]"
        " - [#100020]) ([example_pool docs]) (breaking-change)"
    ),
    "2026.10.0": "https://www.home-assistant.io/blog/2026/10/07/release-202610/",
}


def blog_page(section: str = BREAKING_CHANGES_202611) -> str:
    """A release post holding ``section``, laid out as the blog lays it out."""
    return (
        '<html><body><article class="post">\n'
        "  <h1>2026.11: Placeholder title</h1>\n"
        f"{section}</article>\n"
        '<div id="post-comments"></div></body></html>'
    )


def release(tag: str, body: str | None = None) -> dict[str, Any]:
    """GitHub's listing row for the Core release ``tag``."""
    return {
        "tag_name": tag,
        "name": tag,
        "draft": False,
        "prerelease": "b" in tag,
        "body": _BODIES[tag] if body is None else body,
        "html_url": f"https://github.com/home-assistant/core/releases/tag/{tag}",
    }


def listing(*tags: str) -> list[dict[str, Any]]:
    """GitHub's listing of these Core releases, newest first as given."""
    return [release(tag) for tag in tags]


def patch_release(tag: str) -> dict[str, Any]:
    """A patch release of ``tag`` whose body lists one pull request."""
    number = tag.replace(".", "")
    return release(tag, f"- Fix a placeholder ([@contributor-one] - [#{number}])")
