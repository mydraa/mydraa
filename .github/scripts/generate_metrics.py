#!/usr/bin/env python3
"""Generate GitHub Dark & Light profile metrics SVGs directly from GitHub GraphQL.

Single source of truth: GitHub's `contributionsCollection.contributionCalendar`,
guaranteeing 1:1 parity with the contribution total and daily counts displayed
on the GitHub profile page (including private contributions when enabled).
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import math
import os
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]


GRAPHQL_URL = "https://api.github.com/graphql"

USER_QUERY = """
query($login: String!, $repoCursor: String) {
  user(login: $login) {
    login
    name
    avatarUrl(size: 80)
    createdAt
    followers {
      totalCount
    }
    repositories(
      ownerAffiliations: OWNER
      isFork: false
      first: 100
      after: $repoCursor
      orderBy: { field: UPDATED_AT, direction: DESC }
    ) {
      totalCount
      totalDiskUsage
      pageInfo {
        hasNextPage
        endCursor
      }
      nodes {
        name
        isPrivate
        stargazerCount
        forkCount
        languages(first: 10, orderBy: { field: SIZE, direction: DESC }) {
          edges {
            size
            node {
              name
              color
            }
          }
        }
      }
    }
    repositoriesContributedTo(includeUserRepositories: true) {
      totalCount
    }
    pullRequests {
      totalCount
    }
    issues {
      totalCount
    }
    contributionsCollection {
      hasAnyRestrictedContributions
      restrictedContributionsCount
      totalCommitContributions
      totalIssueContributions
      totalPullRequestContributions
      totalPullRequestReviewContributions
      totalRepositoryContributions
      contributionCalendar {
        totalContributions
        months {
          name
          year
          totalWeeks
          firstDay
        }
        weeks {
          contributionDays {
            date
            contributionCount
            contributionLevel
            weekday
          }
        }
      }
    }
  }
}
"""

MONTH_SHORT = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


@dataclass(frozen=True)
class Theme:
    name: str
    bg: str
    surface: str
    plot_bg: str
    border: str
    border_subtle: str
    text_primary: str
    text_secondary: str
    text_muted: str
    accent: str
    accent_soft_bg: str
    accent_soft_border: str
    accent_grad_start: str
    accent_grad_mid: str
    accent_grad_end: str
    gridline: str
    pill_bg: str
    level_colors: dict[str, str]
    level_strokes: dict[str, str]
    month_bar_colors: tuple[str, str, str, str]
    avatar_ring: str


DARK_THEME = Theme(
    name="dark",
    bg="#0d1117",
    surface="#161b22",
    plot_bg="#0d1117",
    border="#30363d",
    border_subtle="#21262d",
    text_primary="#e6edf3",
    text_secondary="#c9d1d9",
    text_muted="#8b949e",
    accent="#39d353",
    accent_soft_bg="#0e2918",
    accent_soft_border="#1f6f3d",
    accent_grad_start="#238636",
    accent_grad_mid="#39d353",
    accent_grad_end="#58a6ff",
    gridline="#21262d",
    pill_bg="#21262d",
    level_colors={
        "NONE": "#21262d",
        "FIRST_QUARTILE": "#0e4429",
        "SECOND_QUARTILE": "#006d32",
        "THIRD_QUARTILE": "#26a641",
        "FOURTH_QUARTILE": "#39d353",
    },
    level_strokes={
        "NONE": "#30363d",
        "FIRST_QUARTILE": "#1f6f3d",
        "SECOND_QUARTILE": "#238636",
        "THIRD_QUARTILE": "#2ea043",
        "FOURTH_QUARTILE": "#56d364",
    },
    month_bar_colors=("#0e4429", "#006d32", "#26a641", "#39d353"),
    avatar_ring="#30363d",
)

LIGHT_THEME = Theme(
    name="light",
    bg="#ffffff",
    surface="#f6f8fa",
    plot_bg="#ffffff",
    border="#d0d7de",
    border_subtle="#d8dee4",
    text_primary="#1f2328",
    text_secondary="#24292f",
    text_muted="#656d76",
    accent="#1a7f37",
    accent_soft_bg="#dafbe1",
    accent_soft_border="#4ac26b",
    accent_grad_start="#1a7f37",
    accent_grad_mid="#2da44e",
    accent_grad_end="#0969da",
    gridline="#eaeef2",
    pill_bg="#eaeef2",
    level_colors={
        "NONE": "#ebedf0",
        "FIRST_QUARTILE": "#9be9a8",
        "SECOND_QUARTILE": "#40c463",
        "THIRD_QUARTILE": "#30a14e",
        "FOURTH_QUARTILE": "#216e39",
    },
    level_strokes={
        "NONE": "#d8dee4",
        "FIRST_QUARTILE": "#6fdd8b",
        "SECOND_QUARTILE": "#2da44e",
        "THIRD_QUARTILE": "#1f883d",
        "FOURTH_QUARTILE": "#116329",
    },
    month_bar_colors=("#9be9a8", "#40c463", "#30a14e", "#216e39"),
    avatar_ring="#d0d7de",
)


def graphql_request(token: str, query: str, variables: dict[str, Any]) -> dict[str, Any]:
    payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    req = urllib.request.Request(
        GRAPHQL_URL,
        data=payload,
        headers={
            "Authorization": f"bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "mydraa-profile-metrics-generator",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub GraphQL HTTP {exc.code}: {detail}") from exc

    if body.get("errors"):
        raise RuntimeError(f"GitHub GraphQL errors: {json.dumps(body['errors'])}")
    return body["data"]


def fetch_user_data(token: str, login: str) -> dict[str, Any]:
    data = graphql_request(token, USER_QUERY, {"login": login, "repoCursor": None})
    user = data.get("user")
    if not user:
        raise RuntimeError(f"GitHub user '{login}' not found.")

    repos = user["repositories"]
    nodes = list(repos.get("nodes") or [])
    page_info = repos.get("pageInfo") or {}

    while page_info.get("hasNextPage") and page_info.get("endCursor"):
        next_data = graphql_request(
            token,
            USER_QUERY,
            {"login": login, "repoCursor": page_info["endCursor"]},
        )
        next_repos = next_data["user"]["repositories"]
        nodes.extend(next_repos.get("nodes") or [])
        page_info = next_repos.get("pageInfo") or {}

    repos["nodes"] = nodes
    return user


def fetch_avatar_data_uri(url: str | None) -> str | None:
    if not url:
        return None
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "mydraa-profile-metrics-generator"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            content_type = resp.headers.get("Content-Type", "image/png").split(";")[0].strip()
            if not content_type.startswith("image/"):
                return None
            encoded = base64.b64encode(raw).decode("ascii")
            return f"data:{content_type};base64,{encoded}"
    except Exception:
        return None


def parse_date(iso_date: str) -> dt.date:
    return dt.date.fromisoformat(iso_date)


def fmt_short_date(iso_date: str) -> str:
    d = parse_date(iso_date)
    return f"{d.day:02d} {MONTH_SHORT[d.month - 1]}"


def fmt_med_date(iso_date: str) -> str:
    d = parse_date(iso_date)
    return f"{d.day} {MONTH_SHORT[d.month - 1]} {d.year}"


def nice_axis_max(max_val: int) -> tuple[int, list[int]]:
    if max_val <= 0:
        return 4, [0, 2, 4]
    if max_val <= 4:
        return 4, [0, 2, 4]
    if max_val <= 6:
        return 6, [0, 2, 4, 6]
    if max_val <= 9:
        return 9, [0, 3, 6, 9]
    if max_val <= 12:
        return 12, [0, 4, 8, 12]
    if max_val <= 15:
        return 15, [0, 5, 10, 15]
    if max_val <= 20:
        return 20, [0, 5, 10, 15, 20]
    step = int(math.ceil(max_val / 3 / 5.0) * 5)
    top = step * 3
    while top < max_val:
        top += step
    ticks = list(range(0, top + 1, step))
    return top, ticks


def compute_metrics(user: dict[str, Any], timezone_name: str, now: dt.datetime | None = None) -> dict[str, Any]:
    contrib = user["contributionsCollection"]
    calendar = contrib["contributionCalendar"]

    days: list[dict[str, Any]] = []
    for week in calendar.get("weeks") or []:
        for day in week.get("contributionDays") or []:
            days.append(
                {
                    "date": day["date"],
                    "count": int(day["contributionCount"]),
                    "level": day.get("contributionLevel") or "NONE",
                    "weekday": int(day.get("weekday", 0)),
                }
            )
    days.sort(key=lambda d: d["date"])

    # Exact total matching GitHub's profile graph ("X contributions in the last year")
    total_contributions = int(calendar["totalContributions"])

    active_days = sum(1 for d in days if d["count"] > 0)
    peak_day = max(days, key=lambda d: (d["count"], d["date"]), default={"date": "", "count": 0, "level": "NONE"})

    # Longest streak across the rolling calendar
    best_streak = 0
    running = 0
    for d in days:
        if d["count"] > 0:
            running += 1
            if running > best_streak:
                best_streak = running
        else:
            running = 0

    # Current streak: if today has 0 contributions so far, evaluate ending yesterday
    current_streak = 0
    if days:
        idx = len(days) - 1
        if days[idx]["count"] == 0 and idx > 0:
            idx -= 1
        while idx >= 0 and days[idx]["count"] > 0:
            current_streak += 1
            idx -= 1

    # Last 30 days (or fewer if account is brand new)
    last_30 = days[-30:] if len(days) >= 30 else list(days)
    last_30_total = sum(d["count"] for d in last_30)
    last_30_active = sum(1 for d in last_30 if d["count"] > 0)
    last_30_avg = last_30_total / len(last_30) if last_30 else 0.0

    # Monthly aggregation across the exact rolling calendar (sum === totalContributions)
    monthly_map: dict[str, int] = defaultdict(int)
    for d in days:
        ym = d["date"][:7]
        monthly_map[ym] += d["count"]

    monthly: list[dict[str, Any]] = []
    for ym in sorted(monthly_map.keys()):
        year_str, month_str = ym.split("-")
        m_idx = int(month_str) - 1
        monthly.append(
            {
                "ym": ym,
                "label": MONTH_SHORT[m_idx],
                "year": int(year_str),
                "total": monthly_map[ym],
            }
        )

    # Repositories & Languages (owned non-fork repositories visible to token)
    repo_conn = user.get("repositories") or {}
    repo_nodes = repo_conn.get("nodes") or []
    repo_total = int(repo_conn.get("totalCount", len(repo_nodes)))
    contributed_to = int((user.get("repositoriesContributedTo") or {}).get("totalCount", 0))
    prs_total = int((user.get("pullRequests") or {}).get("totalCount", 0))
    issues_total = int((user.get("issues") or {}).get("totalCount", 0))

    lang_sizes: dict[str, int] = defaultdict(int)
    lang_colors: dict[str, str] = {}
    for repo in repo_nodes:
        for edge in ((repo.get("languages") or {}).get("edges") or []):
            size = int(edge.get("size") or 0)
            node = edge.get("node") or {}
            name = node.get("name")
            if name and size > 0:
                lang_sizes[name] += size
                if node.get("color"):
                    lang_colors[name] = node["color"]

    total_lang_bytes = sum(lang_sizes.values())
    sorted_langs = sorted(lang_sizes.items(), key=lambda kv: (-kv[1], kv[0]))
    top_langs: list[dict[str, Any]] = []
    if total_lang_bytes > 0:
        if len(sorted_langs) <= 6:
            chosen = sorted_langs
        else:
            chosen = sorted_langs[:5]
            other_size = sum(sz for _, sz in sorted_langs[5:])
            if other_size > 0:
                chosen.append(("Other", other_size))
        for name, size in chosen:
            top_langs.append(
                {
                    "name": name,
                    "size": size,
                    "pct": (size / total_lang_bytes) * 100.0,
                    "color": lang_colors.get(name, "#8b949e"),
                }
            )

    includes_private = (
        any(bool(r.get("isPrivate")) for r in repo_nodes)
        or bool(contrib.get("hasAnyRestrictedContributions"))
        or int(contrib.get("restrictedContributionsCount") or 0) > 0
    )

    tz = None
    if ZoneInfo is not None:
        try:
            tz = ZoneInfo(timezone_name)
        except Exception:
            tz = dt.timezone.utc
    else:
        tz = dt.timezone.utc

    synced_dt = now.astimezone(tz) if now else dt.datetime.now(tz)
    synced_label = f"{synced_dt.day} {MONTH_SHORT[synced_dt.month - 1]} {synced_dt.year}"

    return {
        "login": user["login"],
        "display_name": user.get("name") or user["login"],
        "avatar_url": user.get("avatarUrl"),
        "total_contributions": total_contributions,
        "active_days": active_days,
        "peak_day": peak_day,
        "best_streak": best_streak,
        "current_streak": current_streak,
        "last_30": last_30,
        "last_30_total": last_30_total,
        "last_30_active": last_30_active,
        "last_30_avg": last_30_avg,
        "monthly": monthly,
        "repo_total": repo_total,
        "contributed_to": contributed_to,
        "prs_total": prs_total,
        "issues_total": issues_total,
        "top_langs": top_langs,
        "total_langs_count": len(sorted_langs),
        "includes_private": includes_private,
        "timezone_name": timezone_name,
        "synced_label": synced_label,
    }


def truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def fmt_int(value: int) -> str:
    return f"{value:,}"


def plural(n: int, singular: str, plural_word: str) -> str:
    return f"{fmt_int(n)} {singular if n == 1 else plural_word}"


def render_svg(metrics: dict[str, Any], theme: Theme, avatar_uri: str | None) -> str:
    W = 840
    H = 528

    login = escape(metrics["login"])
    total_str = fmt_int(metrics["total_contributions"])
    includes_private = metrics["includes_private"]

    # Dynamic hero badge sizing so any contribution count looks crisp and balanced
    num_w = max(40, len(total_str) * 14 + 6)
    label_w = 104
    badge_w = 34 + num_w + label_w + 18
    badge_x = W - 24 - badge_w
    badge_num_x = badge_x + 30
    badge_lbl_x = badge_num_x + num_w + 8

    # Header subtitle
    subtitle = (
        "Rolling 12-month contribution analytics · Includes private contributions"
        if includes_private
        else "Rolling 12-month contribution analytics"
    )

    # Avatar element
    if avatar_uri:
        avatar_svg = (
            f'<clipPath id="avatar-clip"><circle cx="46" cy="48" r="19"/></clipPath>'
            f'<circle cx="46" cy="48" r="20.5" fill="none" stroke="{theme.avatar_ring}" stroke-width="1.5"/>'
            f'<image href="{avatar_uri}" x="27" y="29" width="38" height="38" '
            f'clip-path="url(#avatar-clip)" preserveAspectRatio="xMidYMid slice"/>'
        )
    else:
        initial = escape(metrics["login"][:1].upper())
        avatar_svg = (
            f'<circle cx="46" cy="48" r="20" fill="{theme.accent_soft_bg}" '
            f'stroke="{theme.accent_soft_border}" stroke-width="1.5"/>'
            f'<text x="46" y="54" text-anchor="middle" font-size="16" font-weight="700" '
            f'fill="{theme.accent}">{initial}</text>'
        )

    # Pill tag beside handle
    handle_est_w = max(56, len(metrics["login"]) * 11.5)
    pill_x = 78 + handle_est_w + 10

    # KPI tiles
    peak = metrics["peak_day"]
    peak_sub = (
        f"{fmt_med_date(peak['date'])} · {metrics['active_days']} active"
        if peak.get("count", 0) > 0 and peak.get("date")
        else f"{metrics['active_days']} active days"
    )
    repo_sub_parts = [f"{fmt_int(metrics['contributed_to'])} contributed"]
    if metrics["prs_total"] > 0:
        repo_sub_parts.append(plural(metrics["prs_total"], "PR", "PRs"))
    elif metrics["issues_total"] > 0:
        repo_sub_parts.append(plural(metrics["issues_total"], "issue", "issues"))
    repo_sub = " · ".join(repo_sub_parts)

    kpis = [
        (
            "LAST 30 DAYS",
            fmt_int(metrics["last_30_total"]),
            f"{metrics['last_30_active']} active days · {metrics['last_30_avg']:.1f}/day",
            # Activity pulse icon path (16x16)
            "M1.5 1.75a.75.75 0 00-1.5 0v12.5c0 .414.336.75.75.75h14.5a.75.75 0 000-1.5H1.5V1.75zm14.28 2.53a.75.75 0 00-1.06-1.06L10 7.94 7.53 5.47a.75.75 0 00-1.06 0L3.22 8.72a.75.75 0 001.06 1.06L7 7.06l2.47 2.47a.75.75 0 001.06 0l5.25-5.25z",
        ),
        (
            "BEST STREAK",
            plural(metrics["best_streak"], "day", "days"),
            f"Current: {plural(metrics['current_streak'], 'day', 'days')}",
            # Flame icon path (16x16)
            "M7.998 14.5c2.832 0 5-1.98 5-4.5 0-1.463-.68-2.19-1.879-3.383l-.036-.037c-1.013-1.008-2.3-2.29-2.834-4.434-.322.256-.63.579-.864.953-.432.696-.621 1.58-.046 2.73.473.947.67 2.284-.278 3.232-.61.61-1.545.84-2.403.633a2.788 2.788 0 01-1.436-.874A3.21 3.21 0 003 10c0 2.53 2.164 4.5 4.998 4.5zM9.533.753C9.496.34 9.16.009 8.77.146 7.035.75 4.34 3.187 5.997 6.5c.344.689.285 1.218.003 1.5-.419.419-1.54.487-2.04-.856-.152-.41-.669-.562-.988-.266C2.03 7.756 1.5 8.859 1.5 10c0 3.314 2.91 6 6.5 6s6.5-2.686 6.5-6c0-2.165-.977-3.215-2.327-4.557-1.158-1.15-2.502-2.487-2.64-4.69z",
        ),
        (
            "PEAK DAY",
            fmt_int(peak.get("count", 0)),
            peak_sub,
            # Star/peak icon path (16x16)
            "M8 .25a.75.75 0 01.673.418l1.882 3.815 4.21.612a.75.75 0 01.416 1.279l-3.046 2.97.719 4.192a.75.75 0 01-1.088.791L8 12.347l-3.766 1.98a.75.75 0 01-1.088-.79l.72-4.194L.818 6.374a.75.75 0 01.416-1.28l4.21-.611L7.327.668A.75.75 0 018 .25z",
        ),
        (
            "REPOSITORIES",
            fmt_int(metrics["repo_total"]),
            repo_sub,
            # Full GitHub Octicon repo path (16x16)
            "M2 2.5A2.5 2.5 0 014.5 0h8.75a.75.75 0 01.75.75v12.5a.75.75 0 01-.75.75h-2.5a.75.75 0 110-1.5h1.75v-2h-8a1 1 0 00-.714 1.7.75.75 0 01-1.072 1.05A2.495 2.495 0 012 11.5v-9zm10.5-1V9h-8c-.356 0-.694.074-1 .208V2.5a1 1 0 011-1h8zM5 12.25v3.25a.25.25 0 00.4.2l1.45-1.087a.25.25 0 01.3 0L8.6 15.7a.25.25 0 00.4-.2v-3.25a.25.25 0 00-.25-.25h-3.5a.25.25 0 00-.25.25z",
        ),
    ]

    kpi_parts: list[str] = []
    tile_w = 189
    tile_gap = 12
    for i, (label, val, sub, icon_d) in enumerate(kpis):
        tx = 24 + i * (tile_w + tile_gap)
        ty = 96
        kpi_parts.append(
            f'<g transform="translate({tx},{ty})">'
            f'<rect width="{tile_w}" height="66" rx="8" fill="{theme.surface}" '
            f'stroke="{theme.border_subtle}" stroke-width="1"/>'
            f'<g transform="translate(14,12) scale(0.72)">'
            f'<path fill="{theme.text_muted}" fill-rule="evenodd" d="{icon_d}"/>'
            f'</g>'
            f'<text x="30" y="21" font-size="9.5" font-weight="600" letter-spacing="0.6" '
            f'fill="{theme.text_muted}">{escape(label)}</text>'
            f'<text x="14" y="43" font-size="19" font-weight="700" '
            f'fill="{theme.text_primary}">{escape(val)}</text>'
            f'<text x="14" y="57" font-size="10" fill="{theme.text_muted}">{escape(truncate(sub, 27))}</text>'
            f"</g>"
        )

    # --- Daily Contributions Chart (Last 30 Days) ---
    last_30 = metrics["last_30"]
    max_daily = max((d["count"] for d in last_30), default=0)
    y_max, y_ticks = nice_axis_max(max_daily)

    plot_x0 = 64.0
    plot_x1 = 800.0
    plot_w = plot_x1 - plot_x0
    plot_y_top = 228.0
    plot_y_bot = 319.0
    plot_h = plot_y_bot - plot_y_top

    grid_parts: list[str] = []
    for tick in y_ticks:
        gy = plot_y_bot - (tick / y_max) * plot_h
        dash = "" if tick == 0 else ' stroke-dasharray="3,3"'
        stroke_col = theme.border if tick == 0 else theme.gridline
        grid_parts.append(
            f'<line x1="{plot_x0:.1f}" y1="{gy:.1f}" x2="{plot_x1:.1f}" y2="{gy:.1f}" '
            f'stroke="{stroke_col}" stroke-width="1"{dash}/>'
            f'<text x="{plot_x0 - 8:.1f}" y="{gy + 3.5:.1f}" text-anchor="end" '
            f'font-size="9.5" fill="{theme.text_muted}">{tick}</text>'
        )

    bar_parts: list[str] = []
    x_label_parts: list[str] = []
    n_days = max(1, len(last_30))
    slot_w = plot_w / n_days
    bar_w = min(16.0, max(6.0, slot_w - 8.5))

    label_indices = set()
    if n_days <= 7:
        label_indices = set(range(n_days))
    else:
        step = max(1, round((n_days - 1) / 6))
        for idx in range(0, n_days, step):
            label_indices.add(idx)
        label_indices.add(n_days - 1)
        # Prevent crowding the last two labels
        sorted_idx = sorted(label_indices)
        if len(sorted_idx) >= 2 and (sorted_idx[-1] - sorted_idx[-2]) < 3:
            label_indices.discard(sorted_idx[-2])

    for idx, day in enumerate(last_30):
        cx = plot_x0 + (idx + 0.5) * slot_w
        bx = cx - bar_w / 2.0
        count = day["count"]
        level = day["level"]
        fill = theme.level_colors.get(level, theme.level_colors["FIRST_QUARTILE"])
        stroke = theme.level_strokes.get(level, theme.level_strokes["FIRST_QUARTILE"])
        tip = escape(f"{day['date']}: {plural(count, 'contribution', 'contributions')}")

        if count <= 0:
            by = plot_y_bot - 3.0
            bar_parts.append(
                f'<rect x="{bx:.1f}" y="{by:.1f}" width="{bar_w:.1f}" height="3" rx="1.5" '
                f'fill="{theme.level_colors["NONE"]}"><title>{tip}</title></rect>'
            )
        else:
            bh = 5.0 + (count / y_max) * (plot_h - 5.0)
            by = plot_y_bot - bh
            bar_rx = 3.5 if bh >= 9.0 else 2.2
            is_peak = count == max_daily and max_daily > 0
            lbl_fill = theme.accent if is_peak else theme.text_secondary
            lbl_weight = "700" if is_peak else "600"
            bar_parts.append(
                f'<g><title>{tip}</title>'
                f'<rect x="{bx:.1f}" y="{by:.1f}" width="{bar_w:.1f}" height="{bh:.1f}" rx="{bar_rx:.1f}" '
                f'fill="{fill}" stroke="{stroke}" stroke-width="0.9"/>'
                f'<text x="{cx:.1f}" y="{by - 4.5:.1f}" text-anchor="middle" '
                f'font-size="9.5" font-weight="{lbl_weight}" fill="{lbl_fill}">{count}</text>'
                f"</g>"
            )

        if idx in label_indices:
            is_last = idx == n_days - 1
            lbl_color = theme.text_primary if is_last else theme.text_muted
            lbl_weight = "600" if is_last else "400"
            anchor = "start" if idx == 0 else ("end" if is_last else "middle")
            lx = bx if idx == 0 else (bx + bar_w if is_last else cx)
            x_label_parts.append(
                f'<text x="{lx:.1f}" y="336" text-anchor="{anchor}" '
                f'font-size="9.5" font-weight="{lbl_weight}" fill="{lbl_color}">'
                f"{escape(fmt_short_date(day['date']))}</text>"
            )

    # Legend swatches (exact GitHub 5-level intensity scale, symmetrical spacing)
    legend_levels = ("NONE", "FIRST_QUARTILE", "SECOND_QUARTILE", "THIRD_QUARTILE", "FOURTH_QUARTILE")
    legend_parts: list[str] = [
        f'<text x="692" y="199" text-anchor="end" font-size="9.5" fill="{theme.text_muted}">Less</text>'
    ]
    for li, lv in enumerate(legend_levels):
        sx = 698 + li * 14
        legend_parts.append(
            f'<rect x="{sx}" y="190" width="10" height="10" rx="2.2" '
            f'fill="{theme.level_colors[lv]}" stroke="{theme.level_strokes[lv]}" stroke-width="0.7"/>'
        )
    legend_parts.append(
        f'<text x="770" y="199" text-anchor="start" font-size="9.5" fill="{theme.text_muted}">More</text>'
    )

    # --- Monthly Breakdown (Bottom-Left Panel) ---
    monthly = metrics["monthly"]
    max_month = max((m["total"] for m in monthly), default=0)
    m_x0 = 40.0
    m_x1 = 496.0
    m_w = m_x1 - m_x0
    m_y_top = 414.0
    m_y_bot = 472.0
    m_h = m_y_bot - m_y_top
    n_months = max(1, len(monthly))
    m_slot_w = m_w / n_months
    m_bar_w = min(22.0, max(10.0, m_slot_w - 11.0))

    month_parts: list[str] = [
        f'<line x1="{m_x0:.1f}" y1="{m_y_bot:.1f}" x2="{m_x1:.1f}" y2="{m_y_bot:.1f}" '
        f'stroke="{theme.border}" stroke-width="1"/>'
    ]
    for mi, item in enumerate(monthly):
        cx = m_x0 + (mi + 0.5) * m_slot_w
        bx = cx - m_bar_w / 2.0
        val = item["total"]
        tip = escape(f"{item['label']} {item['year']}: {plural(val, 'contribution', 'contributions')}")
        if val <= 0:
            month_parts.append(
                f'<rect x="{bx:.1f}" y="{m_y_bot - 3.0:.1f}" width="{m_bar_w:.1f}" height="3" rx="1.5" '
                f'fill="{theme.level_colors["NONE"]}"><title>{tip}</title></rect>'
            )
        else:
            ratio = val / max_month if max_month > 0 else 0.0
            bh = 4.5 + ratio * (m_h - 4.5)
            by = m_y_bot - bh
            bar_rx = 3.5 if bh >= 9.0 else 2.0
            if ratio >= 0.75:
                c_idx = 3
            elif ratio >= 0.45:
                c_idx = 2
            elif ratio >= 0.18:
                c_idx = 1
            else:
                c_idx = 0
            fill = theme.month_bar_colors[c_idx]
            stroke_key = ("FIRST_QUARTILE", "SECOND_QUARTILE", "THIRD_QUARTILE", "FOURTH_QUARTILE")[c_idx]
            stroke = theme.level_strokes[stroke_key]
            lbl_fill = theme.accent if val == max_month else theme.text_secondary
            lbl_weight = "700" if val == max_month else "600"
            month_parts.append(
                f'<g><title>{tip}</title>'
                f'<rect x="{bx:.1f}" y="{by:.1f}" width="{m_bar_w:.1f}" height="{bh:.1f}" rx="{bar_rx:.1f}" '
                f'fill="{fill}" stroke="{stroke}" stroke-width="0.8"/>'
                f'<text x="{cx:.1f}" y="{by - 4.5:.1f}" text-anchor="middle" '
                f'font-size="9" font-weight="{lbl_weight}" fill="{lbl_fill}">{val}</text>'
                f"</g>"
            )

        is_latest_m = mi == n_months - 1
        m_lbl_color = theme.text_primary if is_latest_m else theme.text_muted
        m_lbl_weight = "600" if is_latest_m else "400"
        month_parts.append(
            f'<text x="{cx:.1f}" y="486" text-anchor="middle" '
            f'font-size="9" font-weight="{m_lbl_weight}" fill="{m_lbl_color}">'
            f"{escape(item['label'])}</text>"
        )

    # --- Top Languages (Bottom-Right Panel) ---
    top_langs = metrics["top_langs"]
    lang_bar_x = 540.0
    lang_bar_y = 398.0
    lang_bar_w = 260.0
    lang_bar_h = 9.0

    lang_parts: list[str] = []
    if top_langs:
        lang_parts.append(
            f'<clipPath id="lang-bar-clip">'
            f'<rect x="{lang_bar_x:.1f}" y="{lang_bar_y:.1f}" width="{lang_bar_w:.1f}" '
            f'height="{lang_bar_h:.1f}" rx="4.5"/>'
            f"</clipPath>"
            f'<rect x="{lang_bar_x:.1f}" y="{lang_bar_y:.1f}" width="{lang_bar_w:.1f}" '
            f'height="{lang_bar_h:.1f}" rx="4.5" fill="{theme.level_colors["NONE"]}"/>'
        )
        seg_parts: list[str] = []
        cursor_x = lang_bar_x
        gap = 1.5
        usable_w = lang_bar_w - gap * max(0, len(top_langs) - 1)
        for li, lang in enumerate(top_langs):
            seg_w = max(3.0, usable_w * (lang["pct"] / 100.0))
            if li == len(top_langs) - 1:
                seg_w = max(2.0, (lang_bar_x + lang_bar_w) - cursor_x)
            seg_parts.append(
                f'<rect x="{cursor_x:.2f}" y="{lang_bar_y:.1f}" width="{seg_w:.2f}" '
                f'height="{lang_bar_h:.1f}" fill="{escape(lang["color"])}">'
                f'<title>{escape(lang["name"])}: {lang["pct"]:.1f}%</title></rect>'
            )
            cursor_x += seg_w + gap
        lang_parts.append(f'<g clip-path="url(#lang-bar-clip)">{"".join(seg_parts)}</g>')

        # 2-column x 3-row legend with generous 18px center gutter
        for li, lang in enumerate(top_langs[:6]):
            col = li % 2
            row = li // 2
            lx = 540 if col == 0 else 678
            pct_x = 120 if col == 0 else 122
            ly = 428 + row * 24
            lang_parts.append(
                f'<g transform="translate({lx},{ly})">'
                f'<circle cx="4" cy="-3.5" r="4" fill="{escape(lang["color"])}"/>'
                f'<text x="14" y="0" font-size="11" font-weight="500" fill="{theme.text_primary}">'
                f"{escape(truncate(lang['name'], 10))}</text>"
                f'<text x="{pct_x}" y="0" text-anchor="end" font-size="10.5" fill="{theme.text_muted}">'
                f"{lang['pct']:.1f}%</text>"
                f"</g>"
            )
    else:
        lang_parts.append(
            f'<text x="670" y="444" text-anchor="middle" font-size="11" fill="{theme.text_muted}">'
            f"No language data available</text>"
        )

    lang_count_text = (
        plural(metrics["total_langs_count"], "language", "languages")
        if metrics["total_langs_count"] > 0
        else "Codebase"
    )
    footer_left = (
        "Source: GitHub GraphQL contributionCalendar · Private contributions included"
        if includes_private
        else "Source: GitHub GraphQL contributionCalendar"
    )
    footer_right = f"Synced {metrics['synced_label']} · {metrics['timezone_name']}"

    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" aria-label="{login} GitHub Metrics: {total_str} contributions in the last year">
  <title>{login} GitHub Metrics — {total_str} contributions in the last year</title>
  <defs>
    <linearGradient id="top-accent" x1="0%" y1="0%" x2="100%" y2="0%">
      <stop offset="0%" stop-color="{theme.accent_grad_start}"/>
      <stop offset="55%" stop-color="{theme.accent_grad_mid}"/>
      <stop offset="100%" stop-color="{theme.accent_grad_end}"/>
    </linearGradient>
    <clipPath id="card-clip">
      <rect x="1" y="1" width="{W - 2}" height="{H - 2}" rx="12"/>
    </clipPath>
  </defs>
  <style>
    text {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans", Helvetica, Arial, sans-serif;
      font-variant-numeric: tabular-nums;
    }}
  </style>

  <!-- Main Card -->
  <rect x="1" y="1" width="{W - 2}" height="{H - 2}" rx="12" fill="{theme.bg}" stroke="{theme.border}" stroke-width="1"/>
  <rect x="1" y="1" width="{W - 2}" height="3" fill="url(#top-accent)" clip-path="url(#card-clip)"/>

  <!-- Header -->
  <g id="header">
    {avatar_svg}
    <text x="78" y="45" font-size="19" font-weight="700" fill="{theme.text_primary}">{login}</text>
    <rect x="{pill_x:.1f}" y="29" width="96" height="20" rx="10" fill="{theme.pill_bg}" stroke="{theme.border_subtle}" stroke-width="1"/>
    <text x="{pill_x + 48:.1f}" y="42.5" text-anchor="middle" font-size="10.5" font-weight="600" fill="{theme.text_secondary}">GitHub Metrics</text>
    <text x="78" y="64" font-size="11.5" fill="{theme.text_muted}">{escape(subtitle)}</text>

    <!-- Exact GitHub Profile Contribution Total -->
    <rect x="{badge_x}" y="24" width="{badge_w}" height="48" rx="10" fill="{theme.accent_soft_bg}" stroke="{theme.accent_soft_border}" stroke-width="1"/>
    <circle cx="{badge_x + 17}" cy="48" r="4.5" fill="{theme.accent}"/>
    <text x="{badge_num_x}" y="56" font-size="24" font-weight="800" fill="{theme.accent}">{escape(total_str)}</text>
    <text x="{badge_lbl_x}" y="44" font-size="11.5" font-weight="600" fill="{theme.text_primary}">contributions</text>
    <text x="{badge_lbl_x}" y="58" font-size="10.5" fill="{theme.text_muted}">in the last year</text>
  </g>

  <line x1="24" y1="84" x2="{W - 24}" y2="84" stroke="{theme.border_subtle}" stroke-width="1"/>

  <!-- KPI Row -->
  <g id="kpi-row">
    {"".join(kpi_parts)}
  </g>

  <!-- Daily Contributions Chart (Last 30 Days) -->
  <g id="daily-chart">
    <rect x="24" y="176" width="{W - 48}" height="172" rx="10" fill="{theme.surface}" stroke="{theme.border_subtle}" stroke-width="1"/>
    <text x="40" y="199" font-size="13" font-weight="700" fill="{theme.text_primary}">Daily Contributions</text>
    <text x="172" y="199" font-size="11" fill="{theme.text_muted}">·  Last {len(last_30)} days</text>
    {"".join(legend_parts)}
    <rect x="36" y="210" width="{W - 72}" height="129" rx="7" fill="{theme.plot_bg}" stroke="{theme.border_subtle}" stroke-width="0.8"/>
    {"".join(grid_parts)}
    {"".join(bar_parts)}
    {"".join(x_label_parts)}
  </g>

  <!-- Bottom Row: Monthly Activity + Top Languages -->
  <g id="bottom-row">
    <!-- Left: Monthly Contributions -->
    <rect x="24" y="362" width="488" height="134" rx="10" fill="{theme.surface}" stroke="{theme.border_subtle}" stroke-width="1"/>
    <text x="40" y="384" font-size="13" font-weight="700" fill="{theme.text_primary}">Monthly Activity</text>
    <text x="155" y="384" font-size="11" fill="{theme.text_muted}">·  Rolling 12 months</text>
    <text x="496" y="384" text-anchor="end" font-size="11" font-weight="600" fill="{theme.accent}">Total: {escape(total_str)}</text>
    <rect x="34" y="394" width="468" height="96" rx="7" fill="{theme.plot_bg}" stroke="{theme.border_subtle}" stroke-width="0.8"/>
    {"".join(month_parts)}

    <!-- Right: Top Languages -->
    <rect x="524" y="362" width="292" height="134" rx="10" fill="{theme.surface}" stroke="{theme.border_subtle}" stroke-width="1"/>
    <text x="540" y="384" font-size="13" font-weight="700" fill="{theme.text_primary}">Top Languages</text>
    <text x="800" y="384" text-anchor="end" font-size="10.5" fill="{theme.text_muted}">{escape(lang_count_text)}</text>
    {"".join(lang_parts)}
  </g>

  <!-- Footer -->
  <g id="footer">
    <text x="26" y="515" font-size="10" fill="{theme.text_muted}">{escape(footer_left)}</text>
    <text x="{W - 26}" y="515" text-anchor="end" font-size="10" fill="{theme.text_muted}">{escape(footer_right)}</text>
  </g>
</svg>
"""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate GitHub profile metrics SVGs.")
    parser.add_argument("--user", required=True, help="GitHub username.")
    parser.add_argument("--timezone", default="Europe/Paris", help="IANA timezone name for sync date.")
    parser.add_argument("--dark-out", default="github-metrics.svg", help="Output path for dark theme SVG.")
    parser.add_argument("--light-out", default="github-metrics-light.svg", help="Output path for light theme SVG.")
    parser.add_argument("--input-json", help="Optional path to cached GraphQL user JSON (for offline testing).")
    parser.add_argument("--no-avatar-fetch", action="store_true", help="Skip fetching remote avatar image.")
    parser.add_argument("--now", help="Optional ISO-8601 timestamp override for deterministic tests.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.input_json:
        raw = json.loads(Path(args.input_json).read_text(encoding="utf-8"))
        user = raw["data"]["user"] if "data" in raw else raw
    else:
        token = os.environ.get("METRICS_TOKEN") or os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if not token:
            print("Error: METRICS_TOKEN (or GH_TOKEN) environment variable is required.", file=sys.stderr)
            return 1
        user = fetch_user_data(token, args.user)

    now = dt.datetime.fromisoformat(args.now) if args.now else None
    metrics = compute_metrics(user, args.timezone, now=now)
    avatar_uri = None if args.no_avatar_fetch else fetch_avatar_data_uri(metrics.get("avatar_url"))

    dark_svg = render_svg(metrics, DARK_THEME, avatar_uri)
    light_svg = render_svg(metrics, LIGHT_THEME, avatar_uri)

    Path(args.dark_out).write_text(dark_svg, encoding="utf-8")
    Path(args.light_out).write_text(light_svg, encoding="utf-8")

    print(
        f"Generated metrics for @{metrics['login']}: "
        f"{metrics['total_contributions']} contributions (last year), "
        f"{metrics['last_30_total']} in last 30 days, "
        f"best streak {metrics['best_streak']}d, "
        f"current streak {metrics['current_streak']}d."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
