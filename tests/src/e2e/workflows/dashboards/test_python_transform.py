"""E2E tests for python_transform parameter."""

import pytest

from tests.src.e2e.utilities.assertions import MCPAssertions


@pytest.mark.asyncio
async def test_python_transform_simple_update(mcp_client, ha_client):
    """Test simple icon update with python_transform."""
    mcp = MCPAssertions(mcp_client)

    # Create dashboard
    await mcp.call_tool_success(
        "ha_config_set_dashboard",
        {
            "url_path": "test-python",
            "config": {
                "views": [
                    {
                        "cards": [
                            {"type": "button", "entity": "light.test", "icon": "mdi:lamp"}
                        ]
                    }
                ]
            },
        },
    )

    # Get config_hash
    get_result = await mcp.call_tool_success(
        "ha_config_get_dashboard", {"url_path": "test-python"}
    )
    config_hash = get_result["config_hash"]

    # Update with python_transform
    result = await mcp.call_tool_success(
        "ha_config_set_dashboard",
        {
            "url_path": "test-python",
            "config_hash": config_hash,
            "python_transform": "config['views'][0]['cards'][0]['icon'] = 'mdi:lightbulb'",
        },
    )

    assert result["success"] is True
    assert result["action"] == "python_transform"

    # Verify update
    verify = await mcp.call_tool_success(
        "ha_config_get_dashboard", {"url_path": "test-python"}
    )
    assert verify["config"]["views"][0]["cards"][0]["icon"] == "mdi:lightbulb"


@pytest.mark.asyncio
async def test_python_transform_pattern_update(mcp_client, ha_client):
    """Test pattern-based update with python_transform."""
    mcp = MCPAssertions(mcp_client)

    # Create dashboard with multiple light cards
    await mcp.call_tool_success(
        "ha_config_set_dashboard",
        {
            "url_path": "test-python-pattern",
            "config": {
                "views": [
                    {
                        "cards": [
                            {"entity": "light.living_room", "icon": "mdi:lamp"},
                            {"entity": "light.bedroom", "icon": "mdi:lamp"},
                            {"entity": "climate.thermostat", "icon": "mdi:temp"},
                        ]
                    }
                ]
            },
        },
    )

    # Get config_hash
    get_result = await mcp.call_tool_success(
        "ha_config_get_dashboard", {"url_path": "test-python-pattern"}
    )
    config_hash = get_result["config_hash"]

    # Update all lights with pattern
    result = await mcp.call_tool_success(
        "ha_config_set_dashboard",
        {
            "url_path": "test-python-pattern",
            "config_hash": config_hash,
            "python_transform": """
for card in config['views'][0]['cards']:
    if 'light' in card.get('entity', ''):
        card['icon'] = 'mdi:lightbulb-on'
""",
        },
    )

    assert result["success"] is True

    # Verify updates
    verify = await mcp.call_tool_success(
        "ha_config_get_dashboard", {"url_path": "test-python-pattern"}
    )
    cards = verify["config"]["views"][0]["cards"]
    assert cards[0]["icon"] == "mdi:lightbulb-on"  # light.living_room
    assert cards[1]["icon"] == "mdi:lightbulb-on"  # light.bedroom
    assert cards[2]["icon"] == "mdi:temp"  # climate (unchanged)


@pytest.mark.asyncio
async def test_python_transform_blocked_import(mcp_client, ha_client):
    """Test that imports are blocked."""
    mcp = MCPAssertions(mcp_client)

    # Create dashboard
    await mcp.call_tool_success(
        "ha_config_set_dashboard",
        {"url_path": "test-python-security", "config": {"views": [{"cards": []}]}},
    )

    get_result = await mcp.call_tool_success(
        "ha_config_get_dashboard", {"url_path": "test-python-security"}
    )
    config_hash = get_result["config_hash"]

    # Try malicious expression - should fail
    result = await mcp.call_tool_failure(
        "ha_config_set_dashboard",
        {
            "url_path": "test-python-security",
            "config_hash": config_hash,
            "python_transform": "import os; os.system('echo pwned')",
        },
    )
    # Verify error message mentions import or forbidden
    error_msg = result["error"].get("message", str(result["error"])) if isinstance(result["error"], dict) else result["error"]
    assert "import" in error_msg.lower() or "forbidden" in error_msg.lower()


@pytest.mark.asyncio
async def test_python_transform_requires_config_hash(mcp_client, ha_client):
    """Test that python_transform requires config_hash."""
    mcp = MCPAssertions(mcp_client)

    await mcp.call_tool_success(
        "ha_config_set_dashboard", {"url_path": "test-python-hash", "config": {"views": []}}
    )

    # Try without config_hash - should fail
    result = await mcp.call_tool_failure(
        "ha_config_set_dashboard",
        {"url_path": "test-python-hash", "python_transform": "config['views'] = []"},
    )
    # Verify error message mentions config_hash
    error_msg = result["error"].get("message", str(result["error"])) if isinstance(result["error"], dict) else result["error"]
    assert "config_hash" in error_msg.lower()


@pytest.mark.asyncio
async def test_python_transform_mutual_exclusivity(mcp_client, ha_client):
    """Test that python_transform is mutually exclusive with config."""
    mcp = MCPAssertions(mcp_client)

    # Try using both config and python_transform - should fail
    result = await mcp.call_tool_failure(
        "ha_config_set_dashboard",
        {
            "url_path": "test-exclusive",
            "config": {"views": []},
            "python_transform": "config['views'] = []",
        },
    )
    # Verify error message mentions mutual exclusivity
    error_msg = result["error"].get("message", str(result["error"])) if isinstance(result["error"], dict) else result["error"]
    assert "cannot use both" in error_msg.lower() or "mutually exclusive" in error_msg.lower()


@pytest.mark.asyncio
async def test_python_transform_add_card(mcp_client, ha_client):
    """Test adding a card with python_transform."""
    mcp = MCPAssertions(mcp_client)

    # Create dashboard
    await mcp.call_tool_success(
        "ha_config_set_dashboard",
        {"url_path": "test-python-add", "config": {"views": [{"cards": []}]}},
    )

    # Get config_hash
    get_result = await mcp.call_tool_success(
        "ha_config_get_dashboard", {"url_path": "test-python-add"}
    )
    config_hash = get_result["config_hash"]

    # Add card with python_transform
    result = await mcp.call_tool_success(
        "ha_config_set_dashboard",
        {
            "url_path": "test-python-add",
            "config_hash": config_hash,
            "python_transform": "config['views'][0]['cards'].append({'type': 'button', 'entity': 'light.bedroom'})",
        },
    )

    assert result["success"] is True

    # Verify card added
    verify = await mcp.call_tool_success(
        "ha_config_get_dashboard", {"url_path": "test-python-add"}
    )
    cards = verify["config"]["views"][0]["cards"]
    assert len(cards) == 1
    assert cards[0]["type"] == "button"
    assert cards[0]["entity"] == "light.bedroom"


@pytest.mark.asyncio
async def test_python_transform_delete_card(mcp_client, ha_client):
    """Test deleting a card with python_transform."""
    mcp = MCPAssertions(mcp_client)

    # Create dashboard with multiple cards
    await mcp.call_tool_success(
        "ha_config_set_dashboard",
        {
            "url_path": "test-python-delete",
            "config": {
                "views": [
                    {
                        "cards": [
                            {"type": "button", "entity": "light.one"},
                            {"type": "button", "entity": "light.two"},
                            {"type": "button", "entity": "light.three"},
                        ]
                    }
                ]
            },
        },
    )

    # Get config_hash
    get_result = await mcp.call_tool_success(
        "ha_config_get_dashboard", {"url_path": "test-python-delete"}
    )
    config_hash = get_result["config_hash"]

    # Delete middle card
    result = await mcp.call_tool_success(
        "ha_config_set_dashboard",
        {
            "url_path": "test-python-delete",
            "config_hash": config_hash,
            "python_transform": "del config['views'][0]['cards'][1]",
        },
    )

    assert result["success"] is True

    # Verify card deleted
    verify = await mcp.call_tool_success(
        "ha_config_get_dashboard", {"url_path": "test-python-delete"}
    )
    cards = verify["config"]["views"][0]["cards"]
    assert len(cards) == 2
    assert cards[0]["entity"] == "light.one"
    assert cards[1]["entity"] == "light.three"


@pytest.mark.asyncio
async def test_python_transform_hash_conflict(mcp_client, ha_client):
    """Test that hash conflicts are detected."""
    mcp = MCPAssertions(mcp_client)

    # Create dashboard
    await mcp.call_tool_success(
        "ha_config_set_dashboard",
        {"url_path": "test-python-conflict", "config": {"views": [{"cards": []}]}},
    )

    # Get config_hash
    get_result = await mcp.call_tool_success(
        "ha_config_get_dashboard", {"url_path": "test-python-conflict"}
    )
    config_hash = get_result["config_hash"]

    # Modify dashboard directly
    await mcp.call_tool_success(
        "ha_config_set_dashboard",
        {
            "url_path": "test-python-conflict",
            "config": {"views": [{"cards": [{"type": "button"}]}]},
        },
    )

    # Try to use old hash - should fail due to conflict
    result = await mcp.call_tool_failure(
        "ha_config_set_dashboard",
        {
            "url_path": "test-python-conflict",
            "config_hash": config_hash,
            "python_transform": "config['views'][0]['cards'].append({'type': 'tile'})",
        },
    )
    # Verify error message mentions conflict
    error_msg = result["error"].get("message", str(result["error"])) if isinstance(result["error"], dict) else result["error"]
    assert "conflict" in error_msg.lower() or "modified" in error_msg.lower()


@pytest.mark.asyncio
async def test_config_hash_stable_across_reads(mcp_client, ha_client):
    """Test that two consecutive reads of a dashboard return the same config_hash.

    Dashboards (unlike automations) hash the raw HA Lovelace response without
    a normalize-for-roundtrip step, so stability across reads depends on HA
    returning byte-identical responses. This test pins that contract; if HA
    ever introduces non-determinism in the response shape (computed fields,
    ordered-set semantics, etc.), the optimistic-locking surface would
    silently degrade. Mirror of `test_config_hash_stable_across_reads` in
    `automation/test_python_transform.py` and `scripts/test_python_transform.py`.
    See issue #980 for the contract analysis.
    """
    mcp = MCPAssertions(mcp_client)

    # Non-trivial config — multiple views, mixed card types — to exercise
    # any HA-side ordering or normalization differences.
    await mcp.call_tool_success(
        "ha_config_set_dashboard",
        {
            "url_path": "test-hash-stability",
            "config": {
                "views": [
                    {
                        "title": "View 1",
                        "cards": [
                            {"type": "button", "entity": "light.test_a", "icon": "mdi:lamp"},
                            {"type": "entities", "entities": ["light.test_b", "switch.test_c"]},
                        ],
                    },
                    {
                        "title": "View 2",
                        "cards": [
                            {"type": "markdown", "content": "## Hello"},
                        ],
                    },
                ],
            },
        },
    )

    read1 = await mcp.call_tool_success(
        "ha_config_get_dashboard", {"url_path": "test-hash-stability"}
    )
    read2 = await mcp.call_tool_success(
        "ha_config_get_dashboard", {"url_path": "test-hash-stability"}
    )

    assert isinstance(read1["config_hash"], str) and len(read1["config_hash"]) == 16
    assert read1["config_hash"] == read2["config_hash"]


@pytest.mark.asyncio
async def test_python_transform_replace_string_method(mcp_client, ha_client):
    """``str.replace`` works inside ``python_transform``."""
    mcp = MCPAssertions(mcp_client)

    await mcp.call_tool_success(
        "ha_config_set_dashboard",
        {
            "url_path": "test-python-replace",
            "config": {
                "views": [
                    {
                        "cards": [
                            {"type": "markdown", "content": "a\\b\\c"},
                        ]
                    }
                ]
            },
        },
    )

    get_result = await mcp.call_tool_success(
        "ha_config_get_dashboard", {"url_path": "test-python-replace"}
    )

    await mcp.call_tool_success(
        "ha_config_set_dashboard",
        {
            "url_path": "test-python-replace",
            "config_hash": get_result["config_hash"],
            "python_transform": (
                "card = config['views'][0]['cards'][0]\n"
                "card['content'] = card['content'].replace('\\\\', '')"
            ),
        },
    )

    verify = await mcp.call_tool_success(
        "ha_config_get_dashboard", {"url_path": "test-python-replace"}
    )
    assert verify["config"]["views"][0]["cards"][0]["content"] == "abc"


def _hint_suggestions(result: dict) -> list[str]:
    error = result["error"] if isinstance(result["error"], dict) else {}
    return list(error.get("suggestions", []))


@pytest.mark.asyncio
async def test_python_transform_index_error_hints_at_search_mode(mcp_client, ha_client):
    """IndexError from a bad path surfaces the search-mode hint first."""
    mcp = MCPAssertions(mcp_client)

    await mcp.call_tool_success(
        "ha_config_set_dashboard",
        {
            "url_path": "test-python-bad-index",
            "config": {"views": [{"cards": [{"type": "markdown", "content": "x"}]}]},
        },
    )

    get_result = await mcp.call_tool_success(
        "ha_config_get_dashboard", {"url_path": "test-python-bad-index"}
    )

    result = await mcp.call_tool_failure(
        "ha_config_set_dashboard",
        {
            "url_path": "test-python-bad-index",
            "config_hash": get_result["config_hash"],
            "python_transform": "config['views'][3]['cards'][0]['type'] = 'tile'",
        },
    )

    suggestions = _hint_suggestions(result)
    assert suggestions, "expected suggestions in error response"
    assert "card_type" in suggestions[0] and "jq_path" in suggestions[0], (
        f"Expected search-mode hint as first suggestion, got: {suggestions}"
    )


@pytest.mark.asyncio
async def test_python_transform_key_error_hints_at_search_mode(mcp_client, ha_client):
    """KeyError from a missing dict key also surfaces the search-mode hint."""
    mcp = MCPAssertions(mcp_client)

    await mcp.call_tool_success(
        "ha_config_set_dashboard",
        {
            "url_path": "test-python-bad-key",
            "config": {"views": [{"cards": [{"type": "markdown", "content": "x"}]}]},
        },
    )

    get_result = await mcp.call_tool_success(
        "ha_config_get_dashboard", {"url_path": "test-python-bad-key"}
    )

    result = await mcp.call_tool_failure(
        "ha_config_set_dashboard",
        {
            "url_path": "test-python-bad-key",
            "config_hash": get_result["config_hash"],
            # 'sections' doesn't exist on this view — KeyError, not IndexError.
            "python_transform": "config['views'][0]['sections'][0]['cards'][0]['type'] = 'tile'",
        },
    )

    suggestions = _hint_suggestions(result)
    assert suggestions, "expected suggestions in error response"
    assert "card_type" in suggestions[0] and "jq_path" in suggestions[0], (
        f"Expected search-mode hint as first suggestion, got: {suggestions}"
    )


@pytest.mark.asyncio
async def test_python_transform_unrelated_runtime_error_no_search_hint(
    mcp_client, ha_client
):
    """A non-path runtime error (TypeError) must not get the dashboard hint."""
    mcp = MCPAssertions(mcp_client)

    await mcp.call_tool_success(
        "ha_config_set_dashboard",
        {
            "url_path": "test-python-type-error",
            "config": {"views": [{"cards": [{"type": "markdown", "content": "x"}]}]},
        },
    )

    get_result = await mcp.call_tool_success(
        "ha_config_get_dashboard", {"url_path": "test-python-type-error"}
    )

    result = await mcp.call_tool_failure(
        "ha_config_set_dashboard",
        {
            "url_path": "test-python-type-error",
            "config_hash": get_result["config_hash"],
            # str + int is a TypeError at runtime.
            "python_transform": "config['views'][0]['title'] = 'x' + 1",
        },
    )

    suggestions = _hint_suggestions(result)
    joined = " ".join(suggestions)
    assert "card_type" not in joined, (
        f"Unrelated TypeError should not get the search-mode hint, got: {suggestions}"
    )


@pytest.mark.asyncio
async def test_python_transform_returns_authoritative_post_save_hash(
    mcp_client, ha_client
):
    """Regression for #1291: the hash returned by set(python_transform) must
    equal the hash a subsequent get returns — i.e. the post-save authoritative
    state, not the pre-save in-memory transformed dict.

    Before the fix, ``new_config_hash = compute_config_hash(transformed_config)``
    (tools_config_dashboards.py, around L1157) silently drifted whenever HA
    normalised on save (key reorder, default injection, empty-container
    stripping). A subsequent ``set(python_transform=..., config_hash=<returned>)``
    would then trip ``Dashboard modified since last read`` even with no
    concurrent writes. The fix re-fetches via ``_get_dashboard_config_internal``
    to obtain the authoritative hash, matching the sibling pattern in
    ``tools_config_scripts.py`` / ``tools_config_scenes.py`` /
    ``tools_config_automations.py``.

    Hash invariance is a pre-condition for chained python_transform calls and
    for the styleguide's "Dashboard updates use content hashing, not session
    tracking" contract.
    """
    mcp = MCPAssertions(mcp_client)
    url_path = "test-1291-hash-auth"

    try:
        # Create dashboard
        await mcp.call_tool_success(
            "ha_config_set_dashboard",
            {
                "url_path": url_path,
                "config": {
                    "views": [
                        {
                            "cards": [
                                {"type": "markdown", "content": "v1"}
                            ]
                        }
                    ]
                },
            },
        )

        get_initial = await mcp.call_tool_success(
            "ha_config_get_dashboard", {"url_path": url_path}
        )
        initial_hash = get_initial["config_hash"]

        # Apply python_transform; capture returned hash.
        transform_result = await mcp.call_tool_success(
            "ha_config_set_dashboard",
            {
                "url_path": url_path,
                "config_hash": initial_hash,
                "python_transform": (
                    "config['views'][0]['cards'][0]['content'] = 'transformed'"
                ),
            },
        )
        returned_hash = transform_result["config_hash"]

        # Re-read to obtain HA's authoritative post-save hash.
        verify_get = await mcp.call_tool_success(
            "ha_config_get_dashboard", {"url_path": url_path}
        )
        post_save_hash = verify_get["config_hash"]

        # Invariant: hash returned by set(python_transform) must equal a
        # subsequent get's hash. Master computes it from the pre-save dict;
        # the fix re-fetches and computes it from HA's authoritative response.
        assert returned_hash == post_save_hash, (
            "set(python_transform) returned a config_hash that does not match "
            "the hash from a subsequent get — the optimistic-locking chain is "
            "broken (next chained python_transform call would fail with "
            "'Dashboard modified since last read'). "
            f"returned={returned_hash!r} post_save={post_save_hash!r}"
        )

        # Chain a second python_transform using the returned hash — this is
        # the user-visible scenario that breaks when the invariant fails.
        chain_result = await mcp.call_tool_success(
            "ha_config_set_dashboard",
            {
                "url_path": url_path,
                "config_hash": returned_hash,
                "python_transform": (
                    "config['views'][0]['cards'][0]['content'] = 'chained'"
                ),
            },
        )
        assert chain_result["success"] is True
        assert chain_result["action"] == "python_transform"
    finally:
        await mcp.call_tool_success(
            "ha_config_delete_dashboard", {"url_path": url_path}
        )
