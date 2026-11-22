"""What GitHub and the Home Assistant blog serve about Core's releases.

Cut from the 2026.11 release post and from GitHub's release listing of
``home-assistant/core`` as both read on 2026-11-21: the post keeps three of
its backward-incompatible changes, a release body its first lines and every
line tagged ``(breaking-change)``.
"""

from __future__ import annotations

from typing import Any

# The first page of GitHub's listing, as the tool requests it.
RELEASES = (
    "https://api.github.com/repos/home-assistant/core/releases?per_page=100&page=1"
)
BLOG_202611 = "https://www.home-assistant.io/blog/2026/11/04/release-202611/"

# The section heading, its introduction, the AdGuard Home, Alert card (no
# integration link) and Calendar and Sun entries, and the heading after it.
BREAKING_CHANGES_202611 = """<h2>Backward-incompatible changes <a class="title-link" name="backward-incompatible-changes" href="#backward-incompatible-changes"></a>
</h2>
<p>We do our best to avoid making changes to existing functionality that might unexpectedly impact your Home Assistant installation. Unfortunately, sometimes it is inevitable.</p>
<p>We always make sure to document these changes to make the transition as easy as possible for you. This release has the following backward-incompatible changes:</p>
<div class="details-block">
  <details class="details-block-item">
    <summary class="details-block-title">
      <span>AdGuard Home</span>
      <div class="details-block-arrow">
      <svg class="down" width="24" height="24" viewbox="0 0 24 24"><path d="M7.41,8.58L12,13.17L16.59,8.58L18,10L12,16L6,10L7.41,8.58Z"></path></svg>
      <svg class="up" width="24" height="24" viewbox="0 0 24 24"><path d="M7.41,15.41L12,10.83L16.59,15.41L18,14L12,8L6,14L7.41,15.41Z"></path></svg>
      </div>
    </summary>
    <div class="details-block-content" id="content_0">
<p>The <strong>Query log</strong> switch of the AdGuard Home integration now requires AdGuard Home v0.107.30 or newer. On older versions, the switch becomes unavailable.</p>
<p>If AdGuard Home forces HTTPS while the integration is set up without SSL, setup now fails instead of following the redirect to HTTPS. To fix this, remove the integration and add it again with SSL enabled.</p>
<p>(<a rel="external nofollow" target="_blank" href="https://github.com/frenck">@frenck</a> - <a rel="external nofollow" target="_blank" href="https://github.com/home-assistant/core/pull/184349">#184349</a>) (<a href="/integrations/adguard/">AdGuard Home documentation</a>)</p>
</div>
  </details>
</div>
<div class="details-block">
  <details class="details-block-item">
    <summary class="details-block-title">
      <span>Alert card</span>
      <div class="details-block-arrow">
      <svg class="down" width="24" height="24" viewbox="0 0 24 24"><path d="M7.41,8.58L12,13.17L16.59,8.58L18,10L12,16L6,10L7.41,8.58Z"></path></svg>
      <svg class="up" width="24" height="24" viewbox="0 0 24 24"><path d="M7.41,15.41L12,10.83L16.59,15.41L18,14L12,8L6,14L7.41,15.41Z"></path></svg>
      </div>
    </summary>
    <div class="details-block-content" id="content_1">
<p>The Alert card now shows its color and its pulse only while the <span class="terminology">entity<span class="terminology-tooltip">An entity represents a sensor, actor, or function in Home Assistant. Entities are used to monitor physical properties or to control other entities. An entity is usually part of a device or a service.<a class="terminology-link" href="/docs/configuration/entities_domains/"> [Learn more]</a></span></span> is in an alert state, and it supports alarm control panel, binary sensor, cover, and lock entities. The <code>color</code> option is renamed to <code>alert_color</code>, and a new <code>clear_color</code> option sets the color for the rest of the time. Without an <code>alert_color</code>, the card is red during an alert. Set <code>alert_color: none</code> to leave it uncolored.</p>
<p>The card was made for the home and security dashboards, which are not affected. If you added it to a dashboard in <span class="terminology">YAML<span class="terminology-tooltip">YAML is a human-readable data serialization language. It is used to store and transmit data in a structured format. In Home Assistant, YAML is used for configuration, for example in the <code>configuration.yaml</code> or <code>automations.yaml</code> files.<a class="terminology-link" href="/docs/configuration/yaml/"> [Learn more]</a></span></span> yourself, update it to the new options.</p>
<p>(<a rel="external nofollow" target="_blank" href="https://github.com/timmo001">@timmo001</a> - <a rel="external nofollow" target="_blank" href="https://github.com/home-assistant/frontend/pull/54880">frontend#54880</a>)</p>
</div>
  </details>
</div>
<div class="details-block">
  <details class="details-block-item">
    <summary class="details-block-title">
      <span>Calendar and Sun</span>
      <div class="details-block-arrow">
      <svg class="down" width="24" height="24" viewbox="0 0 24 24"><path d="M7.41,8.58L12,13.17L16.59,8.58L18,10L12,16L6,10L7.41,8.58Z"></path></svg>
      <svg class="up" width="24" height="24" viewbox="0 0 24 24"><path d="M7.41,15.41L12,10.83L16.59,15.41L18,14L12,8L6,14L7.41,15.41Z"></path></svg>
      </div>
    </summary>
    <div class="details-block-content" id="content_7">
<p>Sun <span class="terminology">triggers<span class="terminology-tooltip">A trigger is the part of an automation that makes the automation start when something specific happens, for example, when the sun sets, a door opens, or a button is pressed.<a class="terminology-link" href="/docs/automation/trigger/"> [Learn more]</a></span></span> (<code>sun.*</code>) and the <code>calendar.event_started</code> and <code>calendar.event_ended</code> triggers used to default to <code>offset_type: before</code>, so a positive <code>offset</code> fired before the event. The sign of the offset now decides instead: negative fires before the event, positive fires after. If you wrote one of these triggers in <span class="terminology">YAML<span class="terminology-tooltip">YAML is a human-readable data serialization language. It is used to store and transmit data in a structured format. In Home Assistant, YAML is used for configuration, for example in the <code>configuration.yaml</code> or <code>automations.yaml</code> files.<a class="terminology-link" href="/docs/configuration/yaml/"> [Learn more]</a></span></span> with an <code>offset</code> but without <code>offset_type</code>, flip the sign of the offset.</p>
<p>This is an edge case: the UI always wrote <code>offset_type</code>, so triggers created in the UI are not affected.</p>
<p>(<a rel="external nofollow" target="_blank" href="https://github.com/piitaya">@piitaya</a> - <a rel="external nofollow" target="_blank" href="https://github.com/home-assistant/core/pull/184855">#184855</a>) (<a href="/integrations/calendar/">Calendar documentation</a>) (<a href="/integrations/sun/">Sun documentation</a>)</p>
</div>
  </details>
</div>
<h2>All changes <a class="title-link" name="all-changes" href="#all-changes"></a>
</h2>
<p>Of course, there is a lot more in this release. You can find a list of all changes made here: <a href="/changelogs/core-2026.11">Full changelog for Home Assistant Core 2026.11</a>.</p>
"""

_BODIES = {
    "2026.11.2": (
        "- Bump midea-local to v15.1.0 ([@chemelli74] - [#191702]) ([midea docs])"
        " (dependency)\n"
        "- Fix UniFi options flow for unloaded entries ([@Kane610] - [#191684])"
        " ([unifi docs])"
    ),
    "2026.11.1": (
        "- Bump aiounifi to v98 ([@bhamiltoncx] - [#190145]) ([unifi docs])"
        " (dependency)\n"
        "- Fix HomeKit camera motion events never reaching HomeKit ([@RaHehl] -"
        " [#190440]) ([homekit docs])"
    ),
    "2026.11.0": (
        "Home Assistant 2026.11 is here! \U0001f389\n\n"
        "Read all about it in the [release notes]"
        "(https://www.home-assistant.io/blog/2026/11/04/release-202611/).\n"
    ),
    "2026.11.0b6": (
        "- Fix No devices found translation for OKOK Scale (@rrooggiieerr - #190360)"
    ),
    "2026.10.4": (
        "- Raise aiohttp client header limits to 64 KiB ([@MichaelMKKelly] -"
        " [#188329])\n"
        "- Bump pylutron-caseta to 0.30.0 ([@TheJulianJES] - [#188388])"
        " ([lutron_caseta docs]) (dependency)"
    ),
    "2026.10.3": (
        "- Fix Proxmox VE update version detection ([@ezaekiel] - [#184823])"
        " ([proxmoxve docs])"
    ),
    "2026.10.2": (
        "- Fix IndexError on out-of-range heater levels in Tesla Fleet ([@sebr] -"
        " [#178049]) ([tesla_fleet docs])"
    ),
    "2026.10.1": (
        "- Split Matter TemperatureOffset range by Thermostat ClusterRevision"
        " ([@lboue] - [#180517]) ([matter docs])\n"
        "- Remove user-configurable polling interval from `omnilogic` ([@tr4nt0r]"
        " - [#184380]) ([omnilogic docs]) (breaking-change)"
    ),
    "2026.10.0": "https://www.home-assistant.io/blog/2026/10/07/release-202610/",
}


def blog_page(section: str = BREAKING_CHANGES_202611) -> str:
    """A release post holding ``section``, laid out as the blog lays it out."""
    return (
        '<html><body><article class="post">\n'
        "  <h1>2026.11: A lot in store!</h1>\n"
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
