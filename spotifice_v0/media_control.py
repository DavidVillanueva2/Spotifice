#!/usr/bin/env python3

import sys
from time import sleep

import Ice

Ice.loadSlice(f'-I{Ice.getSliceDir()} spotifice_v1.ice')
import Spotifice  # type: ignore # noqa: E402

STATE_NAMES = {
    Spotifice.PlaybackState.PLAYING: 'PLAYING',
    Spotifice.PlaybackState.PAUSED: 'PAUSED',
    Spotifice.PlaybackState.STOPPED: 'STOPPED',
}


def get_proxy(ic, property, cls):
    proxy = ic.propertyToProxy(property)

    for _ in range(5):
        try:
            proxy.ice_ping()
            break
        except Ice.ConnectionRefusedException:
            sleep(0.5)

    object = cls.checkedCast(proxy)
    if object is None:
        raise RuntimeError(f'Invalid proxy for {property}')

    return object


def print_status(render, step):
    status = render.get_status()
    state = STATE_NAMES.get(status.state, status.state)
    track = status.current_track.title if status.current_track else None
    print(f"{step}: {state}, track={track}, repeat={status.is_repeating}")


def main(ic):
    provider = get_proxy(ic, 'Spotifice.MediaProvider.Proxy', Spotifice.MediaProviderPrx)
    render = get_proxy(ic, 'Spotifice.MediaRender.Proxy', Spotifice.MediaRenderPrx)

    print("Fetching all tracks...")
    tracks = provider.get_all_tracks()
    for t in tracks:
        print(f"- {t.title}")

    if len(tracks) < 2:
        print("At least two tracks are required.")
        return

    track1, track2 = tracks[0], tracks[1]

    print(f"Requesting info for track {track1.id}")
    print(f"Track title: {track1.title}")

    render.bind_media_provider(provider)
    render.stop()
    print_status(render, "after stop()")

    print("Loading track into MediaRender...")
    render.load_track(track1.id)
    print_status(render, "after load_track(track1)")

    render.play()
    print_status(render, "after play()")

    render.load_track(track2.id)
    print_status(render, "after load_track(track2)")

    render.pause()
    print_status(render, "after pause()")

    render.previous()
    print_status(render, "after previous()")

    render.play()
    print_status(render, "after play()")

    render.set_repeat(True)
    print_status(render, "after set_repeat(True)")

    render.stop()
    print_status(render, "after stop()")


if __name__ == '__main__':
    if len(sys.argv) < 2:
        sys.exit("Usage: media_control.py --Ice.Config=<config-file>")

    with Ice.initialize(sys.argv) as communicator:
        main(communicator)
