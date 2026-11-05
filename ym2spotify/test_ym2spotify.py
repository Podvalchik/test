import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from spotipy.exceptions import SpotifyException

import ym2spotify as m


def item(name, artists, ms, uri=None):
    return {"name": name, "artists": [{"name": a} for a in artists], "duration_ms": ms, "uri": uri or f"spotify:track:{name}"}


class NormTests(unittest.TestCase):
    def test_strips_feat_punctuation_and_yo(self):
        self.assertEqual(m.norm("Ёлка (feat. Someone) - Song!"), "елка song")
        self.assertEqual(m.norm("Песня [при участии Кто-то]"), "песня")

    def test_keeps_version_markers(self):
        self.assertIn("remix", m.norm("Track (Remix)"))


class ScoreTests(unittest.TestCase):
    ym = m.YmTrack(id="1", title="Smells Like Teen Spirit", artists=["Nirvana"], duration_ms=301000)

    def test_exact_match_scores_high(self):
        s = m.score_candidate(self.ym, item("Smells Like Teen Spirit", ["Nirvana"], 301500))
        self.assertGreater(s, 0.95)

    def test_wrong_artist_is_rejected(self):
        s = m.score_candidate(self.ym, item("Smells Like Teen Spirit", ["Tribute Band"], 301000))
        self.assertLess(s, m.DEFAULT_MIN_SCORE)

    def test_cover_with_different_length_is_rejected(self):
        s = m.score_candidate(self.ym, item("Something Else Entirely", ["Nirvana"], 120000))
        self.assertLess(s, m.DEFAULT_MIN_SCORE)

    def test_version_helps_match_remaster(self):
        ym = m.YmTrack(id="2", title="Song", version="Remastered", artists=["A"], duration_ms=200000)
        s = m.score_candidate(ym, item("Song Remastered", ["A"], 200000))
        self.assertGreater(s, 0.95)


class FindMatchTests(unittest.TestCase):
    ym = m.YmTrack(id="1", title="Song", artists=["Artist"], duration_ms=200000, isrc="USABC1234567")

    def test_isrc_query_goes_first_and_wins(self):
        calls = []

        def search(q):
            calls.append(q)
            return [item("Song", ["Artist"], 200000, "spotify:track:isrc")]

        cand = m.find_match(search, self.ym, m.DEFAULT_MIN_SCORE)
        self.assertEqual(calls, ["isrc:USABC1234567"])
        self.assertEqual((cand.uri, cand.via), ("spotify:track:isrc", "isrc"))

    def test_falls_back_to_text_when_isrc_empty(self):
        def search(q):
            if q.startswith("isrc:"):
                return []
            return [item("Song", ["Artist"], 200500, "spotify:track:text")]

        cand = m.find_match(search, self.ym, m.DEFAULT_MIN_SCORE)
        self.assertEqual(cand.uri, "spotify:track:text")
        self.assertEqual(cand.via, "fielded")

    def test_low_confidence_returns_best_rejected(self):
        cand = m.find_match(lambda q: [item("Other Song", ["Someone"], 100000)], self.ym, m.DEFAULT_MIN_SCORE)
        self.assertIsNotNone(cand)
        self.assertFalse(cand.accepted(m.DEFAULT_MIN_SCORE))

    def test_nothing_found(self):
        self.assertIsNone(m.find_match(lambda q: [], self.ym, m.DEFAULT_MIN_SCORE))

    def test_quotes_in_title_do_not_break_query(self):
        ym = m.YmTrack(id="3", title='Say "Hi"', artists=["A"])
        queries = [q for _, q in m.build_queries(ym)]
        self.assertTrue(all(q.count('"') % 2 == 0 for q in queries))


class BackoffTests(unittest.TestCase):
    def test_retries_on_429_then_succeeds(self):
        err = SpotifyException(429, -1, "limit", headers={"Retry-After": "2"})
        fn = mock.Mock(side_effect=[err, "ok"])
        with mock.patch.object(m.time, "sleep") as sleep:
            self.assertEqual(m.with_backoff(fn), "ok")
        sleep.assert_called_once_with(3)

    def test_long_wait_saves_state_and_exits(self):
        err = SpotifyException(429, -1, "limit", headers={"Retry-After": "5000"})
        saved = mock.Mock()
        with self.assertRaises(SystemExit):
            m.with_backoff(mock.Mock(side_effect=err), on_give_up=saved)
        saved.assert_called_once()

    def test_other_errors_propagate(self):
        err = SpotifyException(403, -1, "forbidden")
        with self.assertRaises(SpotifyException):
            m.with_backoff(mock.Mock(side_effect=err))


class FakeSpotify:
    def __init__(self, catalog, saved=()):
        self.catalog = catalog
        self.saved = set(saved)
        self.added = []

    def search(self, q, type, limit):
        return {"tracks": {"items": self.catalog.get(q, [])}}

    def current_user_saved_tracks_contains(self, tracks):
        return [t in self.saved for t in tracks]

    def current_user_saved_tracks_add(self, tracks):
        self.added.extend(tracks)


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)
        env = {"YANDEX_MUSIC_TOKEN": "t", "SPOTIPY_CLIENT_ID": "i", "SPOTIPY_CLIENT_SECRET": "s"}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tracks = [
            m.YmTrack(id="new", title="B", artists=["Y"], duration_ms=1000, isrc="ISRC_B", liked_at="2024-02-01"),
            m.YmTrack(id="old", title="A", artists=["X"], duration_ms=1000, isrc="ISRC_A", liked_at="2023-01-01"),
            m.YmTrack(id="have", title="C", artists=["Z"], duration_ms=1000, isrc="ISRC_C", liked_at="2023-06-01"),
            m.YmTrack(id="miss", title="D", artists=["W"], duration_ms=1000, isrc="ISRC_D", liked_at="2023-07-01"),
        ]
        self.sp = FakeSpotify(
            {
                "isrc:ISRC_A": [item("A", ["X"], 1000, "spotify:track:a")],
                "isrc:ISRC_B": [item("B", ["Y"], 1000, "spotify:track:b")],
                "isrc:ISRC_C": [item("C", ["Z"], 1000, "spotify:track:c")],
            },
            saved={"spotify:track:c"},
        )

    def tearDown(self):
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def run_main(self, *argv):
        with mock.patch.object(m, "fetch_yandex_likes", return_value=list(self.tracks)), mock.patch.object(
            m, "build_spotify", return_value=self.sp
        ):
            return m.main(list(argv))

    def test_dry_run_writes_report_and_adds_nothing(self):
        self.assertEqual(self.run_main("--dry-run"), 0)
        self.assertEqual(self.sp.added, [])
        report = Path("ym2spotify_report.csv").read_text(encoding="utf-8-sig")
        self.assertIn("not_found", report)
        self.assertEqual(report.count("matched"), 3)

    def test_adds_oldest_first_and_skips_already_saved(self):
        self.assertEqual(self.run_main("--yes"), 0)
        self.assertEqual(self.sp.added, ["spotify:track:a", "spotify:track:b"])

    def test_second_run_uses_cache_and_does_not_search(self):
        self.run_main("--dry-run")
        self.sp.search = mock.Mock(side_effect=AssertionError("search must not be called"))
        self.assertEqual(self.run_main("--dry-run"), 0)

    def test_limit_takes_newest(self):
        self.run_main("--yes", "--limit", "1")
        self.assertEqual(self.sp.added, ["spotify:track:b"])


if __name__ == "__main__":
    unittest.main()
