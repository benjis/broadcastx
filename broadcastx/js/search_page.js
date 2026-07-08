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
