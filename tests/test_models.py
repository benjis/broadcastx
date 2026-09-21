import json
import unittest
from pathlib import Path

from broadcastx.models import (
    extract_broadcasts_from_response,
    extract_media_from_response,
    extract_post_ids_from_response,
)


FIXTURE = Path(__file__).parent / "fixtures" / "user_posts_timeline.json"


class BroadcastExtractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.response = json.loads(FIXTURE.read_text())

    def test_extracts_only_broadcasts_from_target_authored_posts(self):
        broadcasts = extract_broadcasts_from_response(
            self.response,
            "@target_creator",
        )

        self.assertEqual(
            [broadcast.broadcast_id for broadcast in broadcasts],
            ["1TargetOwnABC", "1TargetCardDEF", "1VisibleOwnLMN"],
        )

    def test_preserves_target_post_metadata(self):
        broadcasts = extract_broadcasts_from_response(
            self.response,
            "target_creator",
        )

        first = broadcasts[0]
        self.assertEqual(first.tweet_id, "1000000000000000001")
        self.assertEqual(
            first.tweet_url,
            "https://x.com/target_creator/status/1000000000000000001",
        )
        self.assertEqual(first.user_name, "target_creator")
        self.assertEqual(first.url, "https://x.com/i/broadcasts/1TargetOwnABC")

    def test_author_matching_is_case_insensitive(self):
        broadcasts = extract_broadcasts_from_response(
            self.response,
            "TARGET_CREATOR",
        )

        self.assertEqual(len(broadcasts), 3)

    def test_unknown_target_returns_no_broadcasts(self):
        broadcasts = extract_broadcasts_from_response(
            self.response,
            "missing_creator",
        )

        self.assertEqual(broadcasts, [])

    def test_extracts_target_post_ids_without_counting_nested_other_authors(self):
        post_ids = extract_post_ids_from_response(
            self.response,
            "target_creator",
        )

        self.assertEqual(
            post_ids,
            {
                "1000000000000000001",
                "1000000000000000002",
                "1000000000000000003",
                "1000000000000000004",
                "1000000000000000005",
            },
        )

    def test_uses_response_user_id_when_tweet_omits_embedded_author(self):
        response = {
            "data": {
                "user": {
                    "result": {
                        "rest_id": "9000000000000000001",
                        "legacy": {"screen_name": "target_creator"},
                        "timeline": [
                            {
                                "rest_id": "1000000000000000007",
                                "legacy": {
                                    "id_str": "1000000000000000007",
                                    "user_id_str": "9000000000000000001",
                                    "full_text": "Synthetic ID-attributed broadcast",
                                    "entities": {
                                        "urls": [
                                            {
                                                "expanded_url": "https://x.com/i/broadcasts/1TargetByIdNOP"
                                            }
                                        ]
                                    },
                                },
                            }
                        ],
                    }
                }
            }
        }

        broadcasts = extract_broadcasts_from_response(response, "target_creator")
        post_ids = extract_post_ids_from_response(response, "target_creator")

        self.assertEqual([item.broadcast_id for item in broadcasts], ["1TargetByIdNOP"])
        self.assertEqual(post_ids, {"1000000000000000007"})

    def test_extracts_owned_photo_and_video_but_excludes_reposts(self):
        def tweet(tweet_id, media, *, repost=False):
            legacy = {
                "id_str": tweet_id,
                "full_text": "media post",
                "extended_entities": {"media": media},
            }
            if repost:
                legacy["retweeted_status_result"] = {"result": {}}
            return {
                "rest_id": tweet_id,
                "core": {"user_results": {"result": {"legacy": {"screen_name": "target_creator"}}}},
                "legacy": legacy,
            }

        quoted_inner = tweet("105", [{"id_str": "p4", "type": "photo", "media_url_https": "https://pbs.twimg.com/media/p4.jpg"}])
        quote = tweet("104", [{"id_str": "p3", "type": "photo", "media_url_https": "https://pbs.twimg.com/media/p3.jpg"}])
        quote["quoted_status_result"] = {"result": quoted_inner}
        response = {"tweets": [
            tweet("101", [{"id_str": "p1", "type": "photo", "media_url_https": "https://pbs.twimg.com/media/p1.jpg"}]),
            tweet("102", [{"id_str": "v1", "type": "video"}, {"id_str": "v2", "type": "video"}]),
            tweet("103", [{"id_str": "p2", "type": "photo", "media_url_https": "https://pbs.twimg.com/media/p2.jpg"}], repost=True),
            quote,
        ]}

        media = extract_media_from_response(response, "target_creator")

        self.assertEqual([(item.media_type, item.url) for item in media], [
            ("photo", "https://pbs.twimg.com/media/p1.jpg"),
            ("video", "https://x.com/target_creator/status/102"),
            ("photo", "https://pbs.twimg.com/media/p3.jpg"),
        ])


if __name__ == "__main__":
    unittest.main()
