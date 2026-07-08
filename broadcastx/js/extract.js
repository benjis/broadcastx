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
