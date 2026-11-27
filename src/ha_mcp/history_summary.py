"""Time-per-state totals for recorder history.

Standard library only: the component loads this file by path, so a summary
never imports the server package into Home Assistant.
"""

from typing import Any


def summarize_states(
    states: list[dict[str, Any]],
    window_start_ts: float,
    window_end_ts: float,
    limit: int,
) -> dict[str, Any]:
    """Reduce Core's oldest-first compressed rows to periods and seconds per state.

    Each row lasts until the next row's ``lu`` and the last one until the window
    end; rows after the window end must be dropped first. A row repeating the
    previous state (an attribute-only update, which Core may return) extends
    the current period instead of opening one. Core stamps its window-start
    snapshot with the window start, so the first row counts as a change only
    when it is later than the window start.
    """
    totals: dict[str, dict[str, Any]] = {}
    change_times: list[float] = []
    bounds = [row["lu"] for row in states] + [window_end_ts]
    for index, row in enumerate(states):
        state = row["s"]
        entry = totals.setdefault(state, {"periods": 0, "duration_s": 0.0})
        if index == 0 or state != states[index - 1]["s"]:
            entry["periods"] += 1
            if index or row["lu"] > window_start_ts:
                change_times.append(row["lu"])
        entry["duration_s"] += bounds[index + 1] - bounds[index]
    ranked = sorted(
        totals.items(), key=lambda item: item[1]["duration_s"], reverse=True
    )
    return {
        "changes": len(change_times),
        "first_change": change_times[0] if change_times else None,
        "last_change": change_times[-1] if change_times else None,
        "distinct_states": len(totals),
        "states": {
            state: {
                "periods": entry["periods"],
                "duration_s": round(entry["duration_s"], 3),
            }
            for state, entry in ranked[:limit]
        },
    }
