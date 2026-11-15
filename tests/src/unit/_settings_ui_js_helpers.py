"""DOM builders and result probes shared by the settings page's JS behaviour tests."""

from __future__ import annotations

import re

from ._js_harness import HarnessResult
from ._settings_ui_dom import MIN_DOM


def policy_panel_dom() -> str:
    """MIN_DOM stub plus the elements the policy tab queries.

    The script binds top-level handlers on policy-master-toggle /
    policy-save-global-btn (covered by MIN_DOM), but the bodies of
    policyLoadConfig / policyLoadPending fetch and write into
    policy-* sub-elements that don't appear in MIN_DOM. Add the
    minimum set so the policy-tab invocations don't throw on missing
    elements when we exercise them directly.
    """
    extras = """
      <div id="policy-pending-list" class="backup-empty"></div><div id="policy-pending-announcement"></div>
      <div id="policy-load-error" style="display:none"></div>
      <h3 id="policy-rules-title"></h3>
      <div id="policy-rules-empty" style="display:none"></div>
      <div id="policy-rules-list"></div>
      <select id="policy-rule-effect">
        <option value="require_approval"></option><option value="allow"></option>
      </select>
      <span id="policy-global-save-status"></span>
      <input id="policy-wait-seconds" />
      <input id="policy-ttl-minutes" />
      <input id="policy-event-decisions-toggle" type="checkbox" />
      <input id="policy-decision-pin" type="password" />
      <div class="feature-locked-note" id="policy-pin-status"></div>
      <div class="pin-notice" id="policyUnknownNotice"></div>
      <div class="feature-locked-note" id="policy-master-locked"></div>
      <div class="feature-locked-note" id="policy-manage-tool-locked"></div>
    """
    return MIN_DOM.replace("</body>", extras + "</body>")


def localized_policy_dom(locale: str) -> str:
    """Policy-tab DOM carrying ``locale``'s catalog as the i18n payload."""
    from ha_mcp.settings_ui import build_payload, serialize_payload

    payload = serialize_payload(build_payload(locale))
    element = f'<script id="ha-mcp-i18n" type="application/json">{payload}</script>'
    return policy_panel_dom().replace("</body>", element + "</body>")


def probe(result: HarnessResult, attr: str) -> str | None:
    """Read a ``data-<attr>`` value stamped onto an element in result.dom.

    The JS behaviour tests stash assertion data into data-* attributes
    (live, before the fake clock advances past toast auto-dismiss). This
    pulls the value back out without depending on attribute order.
    """
    m = re.search(rf'data-{attr}="([^"]*)"', result.dom)
    return m.group(1) if m else None
