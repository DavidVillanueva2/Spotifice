#!/usr/bin/env python3

import logging
import signal
import sys
import threading
import uuid
from time import sleep

import Ice
from Ice import identityToString as id2str

from gst_player import GstPlayer

Ice.loadSlice(f'-I{Ice.getSliceDir()} spotifice_v1.ice')
import Spotifice  # type: ignore # noqa: E402

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("MediaRender")


class MediaRenderI(Spotifice.MediaRender):
    def __init__(self, player):
        self.player = player
        self.provider = None
        self.current_track = None
        self.render_id = None
        self.history = []
        self.repeat = False
        self.state = Spotifice.PlaybackState.STOPPED

    def ensure_provider_bound(self):
        if not self.provider:
            raise Spotifice.BadReference(reason="No MediaProvider bound")

    # --- stream lifecycle ---

    def open_and_play(self, render_id):
        try:
            self.provider.open_stream(render_id, self.current_track.id)
        except Spotifice.BadIdentity as e:
            logger.error(f"Error starting stream: {e.reason}")
            raise Spotifice.StreamError(reason="Stream setup failed")

        self.render_id = render_id
        self.player.play(self.chunk_reader(render_id),
                         self.exhausted_handler(render_id))

        if not self.player.confirm_play_starts():
            raise Spotifice.PlayerError(reason="Failed to confirm playback")

        self.state = Spotifice.PlaybackState.PLAYING

    def exhausted_handler(self, render_id):
        def on_track_exhausted():
            if not self.repeat:
                self.state = Spotifice.PlaybackState.STOPPED
                return

            logger.info(f"Repeating track '{self.current_track.id}'")
            try:
                self.open_and_play(render_id)
            except Ice.Exception as e:
                logger.error(f"Error repeating track: {e}")
                self.state = Spotifice.PlaybackState.STOPPED

        return on_track_exhausted

    def teardown_playback(self):
        if self.provider and self.render_id:
            try:
                self.provider.close_stream(self.render_id)
            except Ice.Exception as e:
                logger.error(f"Error closing stream: {e}")

        if not self.player.stop():
            raise Spotifice.PlayerError(reason="Failed to confirm stop")

    def switch_track(self, track):
        previous_state = self.state

        if previous_state != Spotifice.PlaybackState.STOPPED:
            self.teardown_playback()

        self.current_track = track
        logger.info(f"Current track set to: {track.title}")

        if previous_state == Spotifice.PlaybackState.PLAYING:
            self.open_and_play(self.render_id)
        else:
            self.state = previous_state

    # --- RenderConnectivity ---

    def bind_media_provider(self, media_provider, current=None):
        try:
            proxy = media_provider.ice_timeout(500)
            proxy.ice_ping()
        except Ice.ConnectionRefusedException as e:
            raise Spotifice.BadReference(reason=f"MediaProvider not reachable: {e}")

        self.provider = media_provider
        self.history.clear()
        logger.info(f"Bound to MediaProvider '{id2str(media_provider.ice_getIdentity())}'")

    def unbind_media_provider(self, current=None):
        try:
            self.stop(current)
        except Spotifice.PlayerError as e:
            logger.error(f"Unbinding despite stop failure: {e.reason}")

        self.provider = None
        self.history.clear()
        logger.info("Unbound MediaProvider")

    # --- ContentManager ---

    def load_track(self, track_id, current=None):
        self.ensure_provider_bound()

        try:
            track = self.provider.get_track_info(track_id)
        except Spotifice.TrackError as e:
            logger.error(f"Error setting track: {e.reason}")
            raise

        if self.current_track and track.id == self.current_track.id:
            logger.info(f"Track already loaded: {track.title}")
            return

        self.switch_track(track)
        self.history.append(track.id)

    def get_current_track(self, current=None):
        return self.current_track

    # --- PlaybackController ---

    def play(self, current=None):
        assert current, "remote invocation required"

        self.ensure_provider_bound()

        if not self.current_track:
            raise Spotifice.TrackError(reason="No track loaded")

        if self.state == Spotifice.PlaybackState.PLAYING:
            raise Spotifice.PlayerError(reason="Already playing")

        if self.state == Spotifice.PlaybackState.PAUSED and self.player.is_playing():
            if not self.player.resume():
                raise Spotifice.PlayerError(reason="Failed to resume playback")

            self.state = Spotifice.PlaybackState.PLAYING
            logger.info("Resumed")
            return

        self.open_and_play(current.id)

    def pause(self, current=None):
        if self.state == Spotifice.PlaybackState.STOPPED:
            raise Spotifice.PlayerError(reason="Cannot pause when stopped")

        if self.state == Spotifice.PlaybackState.PAUSED:
            return

        if not self.player.pause():
            raise Spotifice.PlayerError(reason="Failed to confirm pause")

        self.state = Spotifice.PlaybackState.PAUSED
        logger.info("Paused")

    def stop(self, current=None):
        if self.state == Spotifice.PlaybackState.STOPPED:
            return

        self.teardown_playback()
        self.state = Spotifice.PlaybackState.STOPPED
        logger.info("Stopped")

    def previous(self, current=None):
        if len(self.history) < 2:
            logger.info("No previous track to go back to")
            return

        self.ensure_provider_bound()

        try:
            track = self.provider.get_track_info(self.history[-2])
        except Spotifice.TrackError as e:
            logger.error(f"Error going back to previous track: {e.reason}")
            raise

        self.switch_track(track)
        self.history.pop()

    def get_status(self, current=None):
        return Spotifice.PlaybackStatus(
            state=self.state,
            current_track=self.current_track,
            is_repeating=self.repeat)

    def set_repeat(self, enabled, current=None):
        self.repeat = enabled
        logger.info(f"Repeat {'enabled' if enabled else 'disabled'}")

    def chunk_reader(self, render_id):
        provider = self.provider

        def read_chunk(chunk_size):
            try:
                return provider.get_chunk(render_id, chunk_size)
            except Spotifice.IOError as e:
                logger.error(f"Playback of track '{e.item}' aborted: {e.reason}")
            except Ice.Exception as e:
                logger.critical(e)

        return read_chunk


def shutdown_on_interrupt(ic):
    def shutdown(*_):
        ic.shutdown()

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGINT,  shutdown)
        signal.signal(signal.SIGTERM, shutdown)


def main(ic, player):
    shutdown_on_interrupt(ic)

    servant = MediaRenderI(player)

    properties = ic.getProperties()
    identity = properties.getPropertyWithDefault(
        'Spotifice.MediaRender.Identity',  str(uuid.uuid1()))

    adapter = ic.createObjectAdapter('Spotifice.MediaRenderAdapter')
    proxy = adapter.add(servant, ic.stringToIdentity(identity))
    logger.info(f"MediaRender: {proxy}")

    adapter.activate()
    while not ic.isShutdown():
        sleep(0.5)

    logger.info("Server shutdown.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("Usage: media_render.py --Ice.Config=<config-file>")

    with Ice.initialize(sys.argv) as communicator, GstPlayer() as player:
        main(communicator, player)
