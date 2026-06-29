"""
Scrape all past broadcasts from a Twitter/X user's timeline.

Uses Playwright to:
1. Open browser and capture auth headers (including x-client-transaction-id)
2. Make paginated GraphQL API calls from within the browser page context
3. Extract broadcast URLs from all tweets

Supports resumable pagination - saves cursor state between runs so you can
continue where you left off after rate limits.
"""

import asyncio
import json
import re
import time
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .config import (
    BROADCAST_PATTERNS,
    normalize_broadcast_url,
)

console = Console()


@dataclass
class BroadcastInfo:
    """Information about a discovered broadcast."""
    broadcast_id: str
    url: str
    tweet_text: str | None = None
    tweet_url: str | None = None
    tweet_id: str | None = None
    created_at: str | None = None
    user_name: str | None = None

    def to_dict(self) -> dict:
        return {k: v for k, v in {
            "broadcast_id": self.broadcast_id,
            "url": self.url,
            "tweet_text": self.tweet_text,
            "tweet_url": self.tweet_url,
            "tweet_id": self.tweet_id,
            "created_at": self.created_at,
            "user_name": self.user_name,
        }.items() if v is not None}


@dataclass
class ScrapeResult:
    """Result of scraping a user's broadcasts."""
    username: str
    broadcasts: list[BroadcastInfo] = field(default_factory=list)
    tweets_scanned: int = 0
    pages_fetched: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "username": self.username,
            "total_broadcasts": len(self.broadcasts),
            "tweets_scanned": self.tweets_scanned,
            "pages_fetched": self.pages_fetched,
            "broadcasts": [b.to_dict() for b in self.broadcasts],
        }


def _state_file(username: str) -> Path:
    return Path("output") / f"{username}_state.json"


def _load_state(username: str) -> dict:
    """Load saved pagination state (cursor, stats) from previous run."""
    path = _state_file(username)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, KeyError):
            pass
    return {}


def _save_state(username: str, user_id: str, cursor: str | None, stats: dict):
    """Save pagination state so we can resume later."""
    path = _state_file(username)
    path.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "username": username,
        "user_id": user_id,
        "last_cursor": cursor,
        "tweets_scanned": stats.get("tweets_scanned", 0),
        "pages_fetched": stats.get("pages_fetched", 0),
        "broadcasts_found": stats.get("broadcasts_found", 0),
        "last_updated": datetime.now().isoformat(),
    }
    path.write_text(json.dumps(state, indent=2))
    console.print(f"  [dim]State saved to {path}[/dim]")


def _extract_broadcasts_from_response(data: dict, username: str) -> list[BroadcastInfo]:
    """Recursively search a Twitter GraphQL JSON response for broadcast URLs."""
    broadcasts = []
    seen_ids: set[str] = set()

    def _check_url(url_str: str, context: dict):
        for pattern in BROADCAST_PATTERNS:
            match = pattern.search(url_str)
            if match:
                bid = match.group(1)
                if bid not in seen_ids:
                    seen_ids.add(bid)
                    tweet_url = None
                    if context.get("tweet_id"):
                        tweet_url = f"https://x.com/{username}/status/{context['tweet_id']}"
                    broadcasts.append(BroadcastInfo(
                        broadcast_id=bid,
                        url=normalize_broadcast_url(url_str) or url_str,
                        tweet_text=context.get("tweet_text"),
                        tweet_url=tweet_url,
                        tweet_id=context.get("tweet_id"),
                        created_at=context.get("created_at"),
                        user_name=username,
                    ))

    def _walk(obj, context=None):
        if context is None:
            context = {}
        if isinstance(obj, dict):
            legacy = obj.get("legacy", {})
            if isinstance(legacy, dict) and legacy.get("full_text"):
                context = {
                    **context,
                    "tweet_text": legacy["full_text"],
                    "tweet_id": legacy.get("id_str") or obj.get("rest_id"),
                    "created_at": legacy.get("created_at"),
                }
            entities = (legacy if isinstance(legacy, dict) else {}).get("entities", {})
            for url_entity in (entities.get("urls", []) if isinstance(entities, dict) else []):
                if isinstance(url_entity, dict):
                    _check_url(url_entity.get("expanded_url", ""), context)
            card = obj.get("card", {})
            if isinstance(card, dict):
                bvs = card.get("legacy", {}).get("binding_values", [])
                for bv in (bvs if isinstance(bvs, list) else []):
                    if isinstance(bv, dict):
                        sval = bv.get("value", {}).get("string_value", "")
                        if sval:
                            _check_url(sval, context)
            for v in obj.values():
                _walk(v, context)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item, context)

    _walk(data)
    return broadcasts


# JS: Make a paginated GraphQL UserTweets call from within the browser page
FETCH_PAGE_JS = """
async ({userId, cursor, hdrs}) => {
    const variables = {
        userId: userId,
        count: 40,
        includePromotedContent: false,
        withQuickPromoteEligibilityTweetFields: true,
        withVoice: true,
        withV2Timeline: true,
    };
    if (cursor) variables.cursor = cursor;

    const features = {
        "rweb_video_screen_enabled": false, "rweb_cashtags_enabled": true,
        "profile_label_improvements_pcf_label_in_post_enabled": true,
        "responsive_web_profile_redirect_enabled": false,
        "rweb_tipjar_consumption_enabled": false, "verified_phone_label_enabled": false,
        "creator_subscriptions_tweet_preview_api_enabled": true,
        "responsive_web_graphql_timeline_navigation_enabled": true,
        "responsive_web_graphql_skip_user_profile_image_extensions_enabled": false,
        "premium_content_api_read_enabled": false,
        "communities_web_enable_tweet_community_results_fetch": true,
        "c9s_tweet_anatomy_moderator_badge_enabled": true,
        "responsive_web_grok_analyze_button_fetch_trends_enabled": false,
        "responsive_web_grok_analyze_post_followups_enabled": true,
        "rweb_cashtags_composer_attachment_enabled": true,
        "responsive_web_jetfuel_frame": true,
        "responsive_web_grok_share_attachment_enabled": true,
        "responsive_web_grok_annotations_enabled": true,
        "articles_preview_enabled": true, "responsive_web_edit_tweet_api_enabled": true,
        "rweb_conversational_replies_downvote_enabled": false,
        "graphql_is_translatable_rweb_tweet_is_translatable_enabled": true,
        "view_counts_everywhere_api_enabled": true,
        "longform_notetweets_consumption_enabled": true,
        "responsive_web_twitter_article_tweet_consumption_enabled": true,
        "content_disclosure_indicator_enabled": true,
        "content_disclosure_ai_generated_indicator_enabled": true,
        "responsive_web_grok_show_grok_translated_post": true,
        "responsive_web_grok_analysis_button_from_backend": true,
        "post_ctas_fetch_enabled": true,
        "freedom_of_speech_not_reach_fetch_enabled": true,
        "standardized_nudges_misinfo": true,
        "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": true,
        "longform_notetweets_rich_text_read_enabled": true,
        "longform_notetweets_inline_media_enabled": false,
        "responsive_web_grok_image_annotation_enabled": true,
        "responsive_web_grok_imagine_annotation_enabled": true,
        "responsive_web_grok_community_note_auto_translation_is_enabled": true,
        "responsive_web_enhance_cards_enabled": false,
    };

    const params = new URLSearchParams();
    params.set('variables', JSON.stringify(variables));
    params.set('features', JSON.stringify(features));
    params.set('fieldToggles', JSON.stringify({withArticlePlainText: false}));

    const h = {
        'x-twitter-active-user': 'yes',
        'x-twitter-client-language': 'en',
        'x-twitter-auth-type': 'OAuth2Session',
    };
    if (hdrs.authorization) h['authorization'] = hdrs.authorization;
    if (hdrs['x-client-transaction-id']) h['x-client-transaction-id'] = hdrs['x-client-transaction-id'];
    if (hdrs['x-csrf-token']) h['x-csrf-token'] = hdrs['x-csrf-token'];

    try {
        const resp = await fetch('/i/api/graphql/RyDU3I9VJtPF-Pnl6vrRlw/UserTweets?' + params.toString(), {
            credentials: 'include', headers: h,
        });
        const retryAfter = resp.headers.get('x-rate-limit-reset');
        if (!resp.ok) return {error: 'HTTP ' + resp.status, data: null, status: resp.status, retryAfter};
        return {error: null, data: await resp.json(), status: 200, retryAfter: null};
    } catch (e) {
        return {error: e.toString(), data: null, status: 0, retryAfter: null};
    }
}
"""

# JS: Extract broadcasts and cursor from a response
EXTRACT_JS = """
(data) => {
    const broadcasts = [];
    const seen = new Set();
    let cursor = null;
    let tweetCount = 0;

    function walk(obj, ctx) {
        if (!obj || typeof obj !== 'object') return;
        const legacy = obj.legacy || {};
        if (legacy.full_text) {
            ctx = {...ctx, tweet_text: legacy.full_text, tweet_id: legacy.id_str || obj.rest_id, created_at: legacy.created_at};
        }
        const urls = ((legacy.entities || {}).urls || []);
        for (const u of urls) {
            const url = u.expanded_url || '';
            const m = url.match(/https?:\\/\\/(?:x|twitter)\\.com\\/i\\/broadcasts\\/([\\w]+)/);
            if (m && !seen.has(m[1])) {
                seen.add(m[1]);
                broadcasts.push({broadcast_id: m[1], url: 'https://x.com/i/broadcasts/' + m[1], tweet_text: ctx.tweet_text || null, tweet_id: ctx.tweet_id || null, created_at: ctx.created_at || null});
            }
        }
        const bvs = ((obj.card || {}).legacy || {}).binding_values || [];
        for (const bv of bvs) {
            const sv = (bv.value || {}).string_value || '';
            const m = sv.match(/https?:\\/\\/(?:x|twitter)\\.com\\/i\\/broadcasts\\/([\\w]+)/);
            if (m && !seen.has(m[1])) {
                seen.add(m[1]);
                broadcasts.push({broadcast_id: m[1], url: 'https://x.com/i/broadcasts/' + m[1], tweet_text: ctx.tweet_text || null, tweet_id: ctx.tweet_id || null, created_at: ctx.created_at || null});
            }
        }
        for (const v of Object.values(obj)) walk(v, ctx);
    }

    try {
        const userResult = (((data || {}).data || {}).user || {}).result || {};
        const tl = userResult.timeline_v2 || userResult.timeline || {};
        const instrs = (tl.timeline || {}).instructions || [];
        for (const inst of instrs) {
            for (const entry of (inst.entries || [])) {
                if ((entry.entryId || '').startsWith('tweet-')) tweetCount++;
                if ((entry.entryId || '').startsWith('cursor-bottom')) {
                    cursor = (entry.content || {}).value || null;
                }
                walk(entry, {});
            }
        }
    } catch(e) {}

    return {broadcasts, cursor, tweetCount};
}
"""

# JS: Make a paginated GraphQL SearchTimeline call
SEARCH_PAGE_JS = """
async ({query, cursor, hdrs}) => {
    const variables = {
        rawQuery: query,
        count: 20,
        product: "Latest",
        querySource: "typed_query",
    };
    if (cursor) variables.cursor = cursor;

    const features = {
        "rweb_video_screen_enabled": false, "rweb_cashtags_enabled": true,
        "profile_label_improvements_pcf_label_in_post_enabled": true,
        "responsive_web_profile_redirect_enabled": false,
        "rweb_tipjar_consumption_enabled": false, "verified_phone_label_enabled": false,
        "creator_subscriptions_tweet_preview_api_enabled": true,
        "responsive_web_graphql_timeline_navigation_enabled": true,
        "responsive_web_graphql_skip_user_profile_image_extensions_enabled": false,
        "premium_content_api_read_enabled": false,
        "communities_web_enable_tweet_community_results_fetch": true,
        "c9s_tweet_anatomy_moderator_badge_enabled": true,
        "responsive_web_grok_analyze_button_fetch_trends_enabled": false,
        "responsive_web_grok_analyze_post_followups_enabled": true,
        "rweb_cashtags_composer_attachment_enabled": true,
        "responsive_web_jetfuel_frame": true,
        "responsive_web_grok_share_attachment_enabled": true,
        "responsive_web_grok_annotations_enabled": true,
        "articles_preview_enabled": true, "responsive_web_edit_tweet_api_enabled": true,
        "rweb_conversational_replies_downvote_enabled": false,
        "graphql_is_translatable_rweb_tweet_is_translatable_enabled": true,
        "view_counts_everywhere_api_enabled": true,
        "longform_notetweets_consumption_enabled": true,
        "responsive_web_twitter_article_tweet_consumption_enabled": true,
        "content_disclosure_indicator_enabled": true,
        "content_disclosure_ai_generated_indicator_enabled": true,
        "responsive_web_grok_show_grok_translated_post": true,
        "responsive_web_grok_analysis_button_from_backend": true,
        "post_ctas_fetch_enabled": true,
        "freedom_of_speech_not_reach_fetch_enabled": true,
        "standardized_nudges_misinfo": true,
        "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": true,
        "longform_notetweets_rich_text_read_enabled": true,
        "longform_notetweets_inline_media_enabled": false,
        "responsive_web_grok_image_annotation_enabled": true,
        "responsive_web_grok_imagine_annotation_enabled": true,
        "responsive_web_grok_community_note_auto_translation_is_enabled": true,
        "responsive_web_enhance_cards_enabled": false,
    };

    const params = new URLSearchParams();
    params.set('variables', JSON.stringify(variables));
    params.set('features', JSON.stringify(features));
    params.set('fieldToggles', JSON.stringify({withArticleRichContentState: false}));

    const h = {
        'x-twitter-active-user': 'yes',
        'x-twitter-client-language': 'en',
        'x-twitter-auth-type': 'OAuth2Session',
    };
    if (hdrs.authorization) h['authorization'] = hdrs.authorization;
    if (hdrs['x-client-transaction-id']) h['x-client-transaction-id'] = hdrs['x-client-transaction-id'];
    if (hdrs['x-csrf-token']) h['x-csrf-token'] = hdrs['x-csrf-token'];

    try {
        const resp = await fetch('/i/api/graphql/dsWn-Op2S0SmJjgY6Yvckg/SearchTimeline?' + params.toString(), {
            credentials: 'include', headers: h,
        });
        const retryAfter = resp.headers.get('x-rate-limit-reset');
        if (!resp.ok) return {error: 'HTTP ' + resp.status, data: null, status: resp.status, retryAfter};
        return {error: null, data: await resp.json(), status: 200, retryAfter: null};
    } catch (e) {
        return {error: e.toString(), data: null, status: 0, retryAfter: null};
    }
}
"""

# JS: Extract broadcasts and cursor from SearchTimeline response
SEARCH_EXTRACT_JS = """
(data) => {
    const broadcasts = [];
    const seen = new Set();
    let cursor = null;
    let tweetCount = 0;

    function walk(obj, ctx) {
        if (!obj || typeof obj !== 'object') return;
        const legacy = obj.legacy || {};
        if (legacy.full_text) {
            ctx = {...ctx, tweet_text: legacy.full_text, tweet_id: legacy.id_str || obj.rest_id, created_at: legacy.created_at};
        }
        const urls = ((legacy.entities || {}).urls || []);
        for (const u of urls) {
            const url = u.expanded_url || '';
            const m = url.match(/https?:\\/\\/(?:x|twitter)\\.com\\/i\\/broadcasts\\/([\\w]+)/);
            if (m && !seen.has(m[1])) {
                seen.add(m[1]);
                broadcasts.push({broadcast_id: m[1], url: 'https://x.com/i/broadcasts/' + m[1], tweet_text: ctx.tweet_text || null, tweet_id: ctx.tweet_id || null, created_at: ctx.created_at || null});
            }
        }
        const bvs = ((obj.card || {}).legacy || {}).binding_values || [];
        for (const bv of bvs) {
            const sv = (bv.value || {}).string_value || '';
            const m = sv.match(/https?:\\/\\/(?:x|twitter)\\.com\\/i\\/broadcasts\\/([\\w]+)/);
            if (m && !seen.has(m[1])) {
                seen.add(m[1]);
                broadcasts.push({broadcast_id: m[1], url: 'https://x.com/i/broadcasts/' + m[1], tweet_text: ctx.tweet_text || null, tweet_id: ctx.tweet_id || null, created_at: ctx.created_at || null});
            }
        }
        for (const v of Object.values(obj)) walk(v, ctx);
    }

    try {
        // SearchTimeline response structure: data.search_by_raw_query.search_timeline.timeline.timeline.instructions
        const searchTimeline = (((data || {}).data || {}).search_by_raw_query || {}).search_timeline || {};
        const tl = searchTimeline.timeline || {};
        const instrs = tl.instructions || [];
        for (const inst of instrs) {
            for (const entry of (inst.entries || [])) {
                if ((entry.entryId || '').startsWith('tweet-')) tweetCount++;
                if ((entry.entryId || '').startsWith('cursor-bottom')) {
                    cursor = (entry.content || {}).value || null;
                }
                walk(entry, {});
            }
        }
    } catch(e) {}

    return {broadcasts, cursor, tweetCount};
}
"""


def _generate_date_windows(start_date: str, end_date: str, step_days: int = 30) -> list[tuple[str, str]]:
    """Generate (since, until) date window pairs for search queries."""
    from datetime import datetime, timedelta
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    windows = []
    current = start
    while current < end:
        window_end = min(current + timedelta(days=step_days), end)
        windows.append((current.strftime("%Y-%m-%d"), window_end.strftime("%Y-%m-%d")))
        current = window_end
    return windows


async def scrape_broadcasts(
    username: str,
    headless: bool = False,
    output_file: Path | str | None = None,
    delay: float = 1.0,
    verbose: bool = False,
    auth_token: str | None = None,
    csrf_token: str | None = None,
    user_id: str | None = None,
) -> ScrapeResult:
    """
    Scrape all past broadcasts using GraphQL API pagination from browser context.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        console.print("[red]Playwright not installed.[/red]")
        return ScrapeResult(username=username, errors=["Playwright not installed"])

    username = username.lstrip("@")
    if output_file is None:
        output_file = Path("output") / f"{username}_broadcasts.json"
    output_file = Path(output_file)

    result = ScrapeResult(username=username)
    all_broadcasts: dict[str, BroadcastInfo] = {}

    scrape_profile = Path.home() / ".broadcastx" / "scrape-profile"
    scrape_profile.mkdir(parents=True, exist_ok=True)

    console.print(f"\n[bold]Scraping broadcasts from @{username} via GraphQL API...[/bold]\n")

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(scrape_profile),
            channel="chrome",
            headless=headless,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )

        page = await context.new_page()

        # Step 1: Open x.com and set cookies
        console.print("[dim]Opening x.com...[/dim]")
        try:
            await page.goto("https://x.com", wait_until="domcontentloaded", timeout=60000)
        except Exception:
            pass

        if "login" in page.url.lower() or not auth_token:
            console.print("[yellow]⚠ Manual login required. Press Enter after logging in...[/yellow]")
            await asyncio.get_event_loop().run_in_executor(None, input)
            await page.wait_for_timeout(3000)
        else:
            await page.evaluate(f"""
                document.cookie = "auth_token={auth_token}; domain=.x.com; path=/; max-age=31536000";
                document.cookie = "ct0={csrf_token}; domain=.x.com; path=/; max-age=31536000";
            """)
            try:
                await page.reload(wait_until="domcontentloaded", timeout=60000)
            except Exception:
                pass
            await page.wait_for_timeout(3000)

        # Step 2: Navigate to profile to capture browser's auth headers
        profile_url = f"https://x.com/{username}"
        console.print(f"[dim]Opening {profile_url} to capture auth headers...[/dim]")

        captured_hdrs = {}

        async def on_request(req):
            nonlocal captured_hdrs
            if "graphql" in req.url and ("UserTweets" in req.url or "UserByScreenName" in req.url):
                h = dict(req.headers)
                if h.get("x-client-transaction-id"):
                    captured_hdrs = h

        page.on("request", on_request)

        try:
            await page.goto(profile_url, wait_until="domcontentloaded", timeout=60000)
        except Exception:
            pass

        # Wait for browser to fire GraphQL request
        for _ in range(20):
            if captured_hdrs.get("x-client-transaction-id"):
                break
            await page.evaluate("window.scrollBy(0, 500)")
            await page.wait_for_timeout(1000)

        if not captured_hdrs.get("x-client-transaction-id"):
            console.print("[red]✗ Could not capture transaction ID.[/red]")
            await context.close()
            result.errors.append("No transaction ID captured")
            return result

        console.print(f"[green]  ✓ Auth headers captured (txn ID: {captured_hdrs['x-client-transaction-id'][:20]}...)[/green]")

        # Step 3: Get user ID if not provided
        if not user_id:
            console.print("[dim]  Looking up user ID...[/dim]")
            try:
                uid_result = await page.evaluate("""
                    async (hdrs) => {
                        const h = {'x-twitter-active-user': 'yes', 'x-twitter-client-language': 'en', 'x-twitter-auth-type': 'OAuth2Session'};
                        if (hdrs.authorization) h['authorization'] = hdrs.authorization;
                        if (hdrs['x-client-transaction-id']) h['x-client-transaction-id'] = hdrs['x-client-transaction-id'];
                        if (hdrs['x-csrf-token']) h['x-csrf-token'] = hdrs['x-csrf-token'];
                        const resp = await fetch('/i/api/graphql/xmU6X_CKVnQ5lSrCbAmJsg/UserByScreenName?variables=' + encodeURIComponent(JSON.stringify({screen_name: '""" + username + """', withSafetyModeUserFields: true})) + '&features=' + encodeURIComponent(JSON.stringify({hidden_profile_subscriptions_enabled: true, rweb_tipjar_consumption_enabled: true, responsive_web_graphql_exclude_directive_enabled: true, verified_phone_label_enabled: false})), {credentials: 'include', headers: h});
                        return await resp.json();
                    }
                """, captured_hdrs)
                user_id = uid_result.get("data", {}).get("user", {}).get("result", {}).get("rest_id")
                if user_id:
                    console.print(f"[green]  ✓ User ID: {user_id}[/green]")
                else:
                    console.print("[red]✗ Could not find user ID.[/red]")
                    await context.close()
                    result.errors.append("User ID not found")
                    return result
            except Exception as e:
                console.print(f"[red]✗ Error: {e}[/red]")
                await context.close()
                result.errors.append(str(e))
                return result
        else:
            console.print(f"[green]  ✓ User ID: {user_id}[/green]")

        # Step 4: Paginate through all tweets via GraphQL API
        console.print(f"\n[bold]Fetching all tweets via GraphQL pagination...[/bold]")

        # Load previous state if available
        state = _load_state(username)
        cursor = state.get("last_cursor")
        if cursor:
            result.tweets_scanned = state.get("tweets_scanned", 0)
            result.pages_fetched = state.get("pages_fetched", 0)
            console.print(f"[green]  ✓ Resuming from page {result.pages_fetched + 1}, {result.tweets_scanned} tweets already scanned[/green]")

        page_num = result.pages_fetched
        consecutive_empty = 0
        max_empty = 5

        while True:
            page_num += 1
            result.pages_fetched = page_num

            try:
                response = await page.evaluate(FETCH_PAGE_JS, {"userId": user_id, "cursor": cursor, "hdrs": captured_hdrs})
            except Exception as e:
                console.print(f"[red]  Page {page_num}: JS error: {e}[/red]")
                consecutive_empty += 1
                if consecutive_empty >= max_empty:
                    break
                await asyncio.sleep(delay * 2)
                continue

            # Handle rate limiting
            status = response.get("status", 0)
            if status == 429:
                retry_after = response.get("retryAfter")
                if retry_after:
                    try:
                        wait_secs = max(int(retry_after) - int(time.time()), 30)
                    except (ValueError, TypeError):
                        wait_secs = 900  # default 15 min
                else:
                    wait_secs = 900

                console.print(f"\n[yellow]⚠ Rate limited! Waiting {wait_secs}s ({wait_secs // 60}m {wait_secs % 60}s)...[/yellow]")
                console.print(f"[yellow]  Cursor saved. Resume by running the same command again.[/yellow]")

                # Save state so we can resume later
                _save_state(username, user_id, cursor, {
                    "tweets_scanned": result.tweets_scanned,
                    "pages_fetched": page_num - 1,
                    "broadcasts_found": len(all_broadcasts),
                })

                # Wait for rate limit to reset
                console.print(f"[dim]  Waiting until {datetime.now().timestamp() + wait_secs:.0f}...[/dim]")
                await asyncio.sleep(wait_secs)

                # Reload the page to get fresh transaction ID
                console.print("[dim]  Refreshing browser session...[/dim]")
                try:
                    await page.reload(wait_until="domcontentloaded", timeout=60000)
                except Exception:
                    pass
                await page.wait_for_timeout(3000)

                # Re-capture auth headers
                new_captured = {}
                async def re_capture(req):
                    nonlocal new_captured
                    if "graphql" in req.url and "UserTweets" in req.url:
                        h = dict(req.headers)
                        if h.get("x-client-transaction-id"):
                            new_captured = h
                page.on("request", re_capture)

                try:
                    await page.goto(profile_url, wait_until="domcontentloaded", timeout=60000)
                except Exception:
                    pass

                for _ in range(15):
                    if new_captured.get("x-client-transaction-id"):
                        captured_hdrs = new_captured
                        break
                    await page.evaluate("window.scrollBy(0, 500)")
                    await page.wait_for_timeout(1000)

                if captured_hdrs.get("x-client-transaction-id"):
                    console.print("[green]  ✓ Fresh auth headers captured, resuming...[/green]")
                else:
                    console.print("[red]  ✗ Could not capture new auth headers.[/red]")
                    break

                continue

            if verbose:
                console.print(f"  [dim]  Page {page_num} response: error={response.get('error')}, hasData={bool(response.get('data'))}[/dim]")

            if response.get("error") and status != 429:
                console.print(f"[yellow]  Page {page_num}: {response['error']}[/yellow]")
                consecutive_empty += 1
                if consecutive_empty >= max_empty:
                    break
                await asyncio.sleep(delay * 2)
                continue

            data = response.get("data")
            if not data or "errors" in (data or {}):
                err = (data or {}).get("errors", [{}])[0].get("message", "empty") if data else "null"
                console.print(f"[yellow]  Page {page_num}: {err}[/yellow]")
                consecutive_empty += 1
                if consecutive_empty >= max_empty:
                    break
                await asyncio.sleep(delay)
                continue

            # Extract broadcasts and cursor
            extracted = await page.evaluate(EXTRACT_JS, data)
            if verbose:
                console.print(f"  [dim]  Extracted: {len(extracted.get('broadcasts',[]))} broadcasts, {extracted.get('tweetCount',0)} tweets, cursor={bool(extracted.get('cursor'))}[/dim]")
            if extracted.get("error"):
                console.print(f"[red]  Extract error: {extracted['error']}[/red]")
            new_count = 0
            for b_data in extracted["broadcasts"]:
                bid = b_data["broadcast_id"]
                if bid not in all_broadcasts:
                    new_count += 1
                    b = BroadcastInfo(
                        broadcast_id=bid,
                        url=b_data["url"],
                        tweet_text=b_data.get("tweet_text"),
                        tweet_url=f"https://x.com/{username}/status/{b_data['tweet_id']}" if b_data.get("tweet_id") else None,
                        tweet_id=b_data.get("tweet_id"),
                        created_at=b_data.get("created_at"),
                        user_name=username,
                    )
                    all_broadcasts[bid] = b
                    preview = (b.tweet_text[:60] + "...") if b.tweet_text and len(b.tweet_text) > 60 else (b.tweet_text or "")
                    console.print(f"  [green]Found:[/green] {b.url}  [dim]{preview}[/dim]")

            tweet_count = extracted["tweetCount"]
            next_cursor = extracted["cursor"]
            result.tweets_scanned += tweet_count

            if tweet_count > 0 or new_count > 0:
                consecutive_empty = 0
            else:
                consecutive_empty += 1

            console.print(
                f"  [dim]Page {page_num}: {tweet_count} tweets, {new_count} new | "
                f"Total: {result.tweets_scanned} tweets, {len(all_broadcasts)} broadcasts[/dim]"
            )

            if not next_cursor:
                console.print("[dim]  End of timeline.[/dim]")
                break

            if consecutive_empty >= max_empty:
                console.print(f"[dim]  {max_empty} empty pages, stopping.[/dim]")
                break

            cursor = next_cursor

            # Save state after each successful page
            _save_state(username, user_id, cursor, {
                "tweets_scanned": result.tweets_scanned,
                "pages_fetched": page_num,
                "broadcasts_found": len(all_broadcasts),
            })

            await asyncio.sleep(delay)

        # Phase 2: SearchTimeline fallback for older tweets
        # Generate date windows from 2020-01-01 to today
        from datetime import datetime, timedelta
        today = datetime.now().strftime("%Y-%m-%d")
        date_windows = _generate_date_windows("2020-01-01", today, step_days=30)

        console.print(f"\n[bold]Phase 2: SearchTimeline ({len(date_windows)} date windows)...[/bold]")

        # Track already-seen tweet IDs to deduplicate
        seen_tweet_ids: set[str] = set()
        for b in all_broadcasts.values():
            if b.tweet_id:
                seen_tweet_ids.add(b.tweet_id)

        for i, (since, until) in enumerate(date_windows):
            query = f"from:{username} since:{since} until:{until}"
            console.print(f"  [dim]Window {i+1}/{len(date_windows)}: {since} to {until}[/dim]")

            search_cursor = None
            window_empty = 0

            while True:
                try:
                    response = await page.evaluate(SEARCH_PAGE_JS, {"query": query, "cursor": search_cursor, "hdrs": captured_hdrs})
                except Exception as e:
                    console.print(f"    [red]JS error: {e}[/red]")
                    break

                status = response.get("status", 0)
                if status == 429:
                    retry_after = response.get("retryAfter")
                    try:
                        wait_secs = max(int(retry_after) - int(time.time()), 30) if retry_after else 900
                    except (ValueError, TypeError):
                        wait_secs = 900
                    console.print(f"    [yellow]Rate limited, waiting {wait_secs//60}m...[/yellow]")
                    _save_state(username, user_id, search_cursor, {
                        "tweets_scanned": result.tweets_scanned,
                        "pages_fetched": result.pages_fetched,
                        "broadcasts_found": len(all_broadcasts),
                    })
                    await asyncio.sleep(wait_secs)
                    # Refresh session
                    try:
                        await page.reload(wait_until="domcontentloaded", timeout=60000)
                    except Exception:
                        pass
                    await page.wait_for_timeout(3000)
                    # Re-capture headers
                    new_captured = {}
                    async def re_capture_search(req):
                        nonlocal new_captured
                        if "graphql" in req.url and "SearchTimeline" in req.url:
                            h = dict(req.headers)
                            if h.get("x-client-transaction-id"):
                                new_captured = h
                    page.on("request", re_capture_search)
                    try:
                        await page.goto(profile_url, wait_until="domcontentloaded", timeout=60000)
                    except Exception:
                        pass
                    for _ in range(15):
                        if new_captured.get("x-client-transaction-id"):
                            captured_hdrs = new_captured
                            break
                        await page.evaluate("window.scrollBy(0, 500)")
                        await page.wait_for_timeout(1000)
                    continue

                if response.get("error"):
                    console.print(f"    [yellow]{response['error']}[/yellow]")
                    break

                data = response.get("data")
                if not data:
                    break

                extracted = await page.evaluate(SEARCH_EXTRACT_JS, data)
                new_count = 0
                for b_data in extracted["broadcasts"]:
                    bid = b_data["broadcast_id"]
                    tid = b_data.get("tweet_id")
                    if bid not in all_broadcasts and (not tid or tid not in seen_tweet_ids):
                        new_count += 1
                        b = BroadcastInfo(
                            broadcast_id=bid,
                            url=b_data["url"],
                            tweet_text=b_data.get("tweet_text"),
                            tweet_url=f"https://x.com/{username}/status/{tid}" if tid else None,
                            tweet_id=tid,
                            created_at=b_data.get("created_at"),
                            user_name=username,
                        )
                        all_broadcasts[bid] = b
                        if tid:
                            seen_tweet_ids.add(tid)
                        preview = (b.tweet_text[:60] + "...") if b.tweet_text and len(b.tweet_text) > 60 else (b.tweet_text or "")
                        console.print(f"    [green]Found:[/green] {b.url}  [dim]{preview}[/dim]")

                tweet_count = extracted["tweetCount"]
                result.tweets_scanned += tweet_count

                next_cursor = extracted["cursor"]
                if tweet_count > 0 or new_count > 0:
                    window_empty = 0
                else:
                    window_empty += 1

                if not next_cursor or window_empty >= 3:
                    break

                search_cursor = next_cursor
                await asyncio.sleep(delay)

            # Save state after each window
            _save_state(username, user_id, None, {
                "tweets_scanned": result.tweets_scanned,
                "pages_fetched": result.pages_fetched,
                "broadcasts_found": len(all_broadcasts),
            })

        await context.close()

    result.broadcasts = list(all_broadcasts.values())
    _save_results(result, output_file)
    _print_summary(result)
    return result


def _save_results(result: ScrapeResult, output_file: Path):
    output_file.parent.mkdir(parents=True, exist_ok=True)
    existing = {"broadcasts": []}
    if output_file.exists():
        try:
            existing = json.loads(output_file.read_text())
        except (json.JSONDecodeError, KeyError):
            pass
    existing_ids = {b["broadcast_id"] for b in existing.get("broadcasts", []) if isinstance(b, dict)}
    new_entries = [b.to_dict() for b in result.broadcasts if b.broadcast_id not in existing_ids]
    broadcasts_list = existing.get("broadcasts", []) + new_entries
    data = {
        "username": result.username,
        "total_broadcasts": len(broadcasts_list),
        "tweets_scanned": result.tweets_scanned,
        "pages_fetched": result.pages_fetched,
        "broadcasts": broadcasts_list,
    }
    output_file.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    console.print(f"\n  [dim]Saved to {output_file}[/dim]")


def _print_summary(result: ScrapeResult):
    table = Table(title=f"Broadcasts from @{result.username}")
    table.add_column("ID", style="cyan")
    table.add_column("URL", style="blue")
    table.add_column("Tweet ID", style="dim")
    table.add_column("Tweet Preview", style="dim", max_width=40)
    table.add_column("Date", style="green")
    for b in result.broadcasts:
        preview = (b.tweet_text[:35] + "...") if b.tweet_text and len(b.tweet_text) > 35 else (b.tweet_text or "—")
        table.add_row(b.broadcast_id, b.url, b.tweet_id or "—", preview, b.created_at or "—")
    console.print()
    console.print(table)
    console.print(f"\n[bold]{len(result.broadcasts)}[/bold] broadcast(s) found | {result.tweets_scanned} tweets scanned | {result.pages_fetched} pages")
