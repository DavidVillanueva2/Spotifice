from pathlib import Path
from time import sleep

from gst_player import GstPlayer
from media_provider import main as provider_main
from media_render import Spotifice
from media_render import main as render_main
from metadata import identify

from .icetest import IceTestCase, pool_condition

MEDIA = Path('test/media')

TRACK1 = identify(MEDIA / '1s.mp3')
TRACK2 = identify(MEDIA / '2s.mp3')
TRACK3 = identify(MEDIA / '4s.mp3')

PLAYING = Spotifice.PlaybackState.PLAYING
PAUSED = Spotifice.PlaybackState.PAUSED
STOPPED = Spotifice.PlaybackState.STOPPED


class TestRenderV1(IceTestCase):
    provider_port = 10000
    provider_identity = 'mediaProvider0'
    render_port = 10001
    render_identity = 'mediaRender0'

    def setUp(self):
        provider_props = {
            'Spotifice.MediaProviderAdapter.Endpoints': f'tcp -p {self.provider_port}',
            'Spotifice.MediaProvider.Content': str(MEDIA),
            'Spotifice.MediaProvider.Identity': self.provider_identity
        }
        provider_strprx = f'{self.provider_identity}:tcp -p {self.provider_port} -t 500'
        self.create_server(provider_main, provider_props)

        player = GstPlayer()
        player.start()
        self.addCleanup(player.shutdown)

        render_props = {
            'Spotifice.MediaRenderAdapter.Endpoints': f'tcp -p {self.render_port}',
            'Spotifice.MediaRender.Identity': self.render_identity
        }
        render_strprx = f'{self.render_identity}:tcp -p {self.render_port} -t 500'
        self.create_server(render_main, render_props, player)

        self.provider = self.create_proxy(provider_strprx, Spotifice.MediaProviderPrx)
        self.sut = self.create_proxy(render_strprx, Spotifice.MediaRenderPrx)

    # --- helpers ---

    def bind(self):
        self.sut.bind_media_provider(self.provider)

    def load(self, track_id):
        self.sut.load_track(track_id)

    def state(self):
        return self.sut.get_status().state

    def current_title(self):
        track = self.sut.get_status().current_track
        return track.title if track else None


class StatusTests(TestRenderV1):
    def test_initial_status(self):
        status = self.sut.get_status()

        self.assertEqual(status.state, STOPPED)
        self.assertIsNone(status.current_track)
        self.assertFalse(status.is_repeating)

    def test_repeat_is_disabled_by_default(self):
        self.bind()

        self.assertFalse(self.sut.get_status().is_repeating)

    def test_set_repeat_updates_status(self):
        self.bind()

        self.sut.set_repeat(True)
        self.assertTrue(self.sut.get_status().is_repeating)

        self.sut.set_repeat(False)
        self.assertFalse(self.sut.get_status().is_repeating)


class ErrorTests(TestRenderV1):
    def test_play_without_provider(self):
        with self.assertRaises(Spotifice.BadReference) as cm:
            self.sut.play()

        self.assertEqual(cm.exception.reason, "No MediaProvider bound")

    def test_play_without_track(self):
        self.bind()

        with self.assertRaises(Spotifice.TrackError) as cm:
            self.sut.play()

        self.assertEqual(cm.exception.reason, "No track loaded")

    def test_play_while_playing(self):
        self.bind()
        self.load(TRACK3)
        self.sut.play()

        with self.assertRaises(Spotifice.PlayerError) as cm:
            self.sut.play()

        self.assertEqual(cm.exception.reason, "Already playing")

    def test_pause_while_stopped(self):
        self.bind()

        with self.assertRaises(Spotifice.PlayerError) as cm:
            self.sut.pause()

        self.assertEqual(cm.exception.reason, "Cannot pause when stopped")


class PauseTests(TestRenderV1):
    def test_pause_sets_paused_state(self):
        self.bind()
        self.load(TRACK3)
        self.sut.play()

        self.sut.pause()

        self.assertEqual(self.state(), PAUSED)

    def test_pause_is_idempotent(self):
        self.bind()
        self.load(TRACK3)
        self.sut.play()

        self.sut.pause()
        self.sut.pause()

        self.assertEqual(self.state(), PAUSED)

    def test_play_resumes_paused_playback(self):
        self.bind()
        self.load(TRACK3)
        self.sut.play()
        self.sut.pause()

        self.sut.play()

        self.assertEqual(self.state(), PLAYING)
        self.assertEqual(self.current_title(), '4s')


class StopTests(TestRenderV1):
    def test_stop_when_stopped_is_idempotent(self):
        self.bind()

        self.sut.stop()
        self.sut.stop()

        self.assertEqual(self.state(), STOPPED)

    def test_stop_keeps_the_current_track(self):
        self.bind()
        self.load(TRACK3)
        self.sut.play()

        self.sut.stop()

        self.assertEqual(self.state(), STOPPED)
        self.assertEqual(self.current_title(), '4s')

    def test_play_after_stop_plays_again(self):
        self.bind()
        self.load(TRACK3)
        self.sut.play()
        self.sut.stop()

        self.sut.play()

        self.assertEqual(self.state(), PLAYING)

    def test_stop_from_paused(self):
        self.bind()
        self.load(TRACK3)
        self.sut.play()
        self.sut.pause()

        self.sut.stop()

        self.assertEqual(self.state(), STOPPED)


class LoadTrackTests(TestRenderV1):
    def test_load_track_when_stopped_stays_stopped(self):
        self.bind()

        self.load(TRACK1)

        self.assertEqual(self.state(), STOPPED)
        self.assertEqual(self.current_title(), '1s')

    def test_load_track_while_playing_keeps_playing(self):
        self.bind()
        self.load(TRACK3)
        self.sut.play()

        self.load(TRACK2)

        self.assertEqual(self.state(), PLAYING)
        self.assertEqual(self.current_title(), '2s')

    def test_load_track_while_paused_keeps_paused(self):
        self.bind()
        self.load(TRACK3)
        self.sut.play()
        self.sut.pause()

        self.load(TRACK2)

        self.assertEqual(self.state(), PAUSED)
        self.assertEqual(self.current_title(), '2s')

    def test_load_wrong_track_keeps_state(self):
        self.bind()
        self.load(TRACK3)
        self.sut.play()

        with self.assertRaises(Spotifice.TrackError):
            self.load('bad-track-id')

        self.assertEqual(self.state(), PLAYING)
        self.assertEqual(self.current_title(), '4s')


class RepeatTests(TestRenderV1):
    def test_track_end_stops_player_when_repeat_is_disabled(self):
        self.bind()
        self.load(TRACK1)
        self.sut.play()

        self.assertEqual(self.state(), PLAYING)

        stopped = pool_condition(lambda: self.state() == STOPPED, ntries=8)

        self.assertIsNotNone(stopped)
        self.assertEqual(self.current_title(), '1s')

    def test_track_is_repeated_when_enabled(self):
        self.bind()
        self.load(TRACK1)
        self.sut.set_repeat(True)
        self.sut.play()

        sleep(4)

        self.assertEqual(self.state(), PLAYING)
        self.assertEqual(self.current_title(), '1s')


class HistoryTests(TestRenderV1):
    def test_history_back_to_first_track(self):
        self.bind()
        self.load(TRACK1)
        self.load(TRACK2)

        self.sut.previous()

        self.assertEqual(self.current_title(), '1s')

    def test_history_walks_back_step_by_step(self):
        self.bind()
        self.load(TRACK1)
        self.load(TRACK2)
        self.load(TRACK3)

        self.sut.previous()
        self.sut.previous()

        self.assertEqual(self.current_title(), '1s')

    def test_history_does_not_return_to_discarded_track(self):
        self.bind()
        self.load(TRACK1)
        self.load(TRACK2)

        self.sut.previous()
        self.sut.previous()

        self.assertEqual(self.current_title(), '1s')

    def test_reloading_current_track_does_not_change_history(self):
        self.bind()
        self.load(TRACK1)
        self.load(TRACK2)
        self.load(TRACK2)

        self.sut.previous()

        self.assertEqual(self.current_title(), '1s')

    def test_history_after_previous_and_new_track(self):
        self.bind()
        self.load(TRACK1)
        self.load(TRACK2)

        self.sut.previous()
        self.load(TRACK3)
        self.sut.previous()

        self.assertEqual(self.current_title(), '1s')

    def test_previous_without_previous_track_does_nothing(self):
        self.bind()
        self.load(TRACK1)

        self.sut.previous()

        self.assertEqual(self.current_title(), '1s')

    def test_previous_while_playing_keeps_playing(self):
        self.bind()
        self.load(TRACK3)
        self.sut.play()
        self.load(TRACK1)

        self.sut.previous()

        self.assertEqual(self.state(), PLAYING)
        self.assertEqual(self.current_title(), '4s')

    def test_previous_while_paused_keeps_paused(self):
        self.bind()
        self.load(TRACK3)
        self.sut.play()
        self.sut.pause()
        self.load(TRACK1)

        self.sut.previous()

        self.assertEqual(self.state(), PAUSED)
        self.assertEqual(self.current_title(), '4s')

    def test_previous_while_stopped_keeps_stopped(self):
        self.bind()
        self.load(TRACK3)
        self.load(TRACK1)

        self.sut.previous()

        self.assertEqual(self.state(), STOPPED)
        self.assertEqual(self.current_title(), '4s')

    def test_history_is_cleared_on_bind(self):
        self.bind()
        self.load(TRACK1)
        self.load(TRACK2)

        self.sut.unbind_media_provider()
        self.bind()
        self.load(TRACK3)

        self.sut.previous()

        self.assertEqual(self.current_title(), '4s')

    def test_unbind_stops_playback(self):
        self.bind()
        self.load(TRACK3)
        self.sut.play()

        self.sut.unbind_media_provider()

        self.assertEqual(self.state(), STOPPED)
