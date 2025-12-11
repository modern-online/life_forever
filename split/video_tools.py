import os, time, contextlib
import vlc

inst = None
player = None
em = None
_end_evt_attached = False

_intro_ml = None
_intro_mlp = None
_current_media_name = None
_main_xid = None
_on_end_cb = None

def init_video(main_xid, on_end_callback):
    """Initialize VLC instance and player; register end-of-media callback."""
    global inst, player, em, _main_xid, _on_end_cb
    _main_xid = int(main_xid)
    _on_end_cb = on_end_callback
    inst = vlc.Instance("--no-video-title-show", "--quiet", "--verbose=-1")
    player = inst.media_player_new()
    player.set_xwindow(_main_xid)
    em = player.event_manager()

def _evt_end(event):
    global _current_media_name
    print(f"[END] file={_current_media_name}", flush=True)
    if _on_end_cb:
        _on_end_cb()

def _attach_end_evt():
    global _end_evt_attached
    if em is None:
        return
    if not _end_evt_attached:
        em.event_attach(vlc.EventType.MediaPlayerEndReached, _evt_end)
        _end_evt_attached = True

def detach_end_evt():
    global _end_evt_attached
    if em is None:
        return
    if _end_evt_attached:
        with contextlib.suppress(Exception):
            em.event_detach(vlc.EventType.MediaPlayerEndReached, _evt_end)
        _end_evt_attached = False

def stop_intro_loop():
    """Stop and release any running intro MediaListPlayer."""
    global _intro_mlp, _intro_ml
    if _intro_mlp:
        with contextlib.suppress(Exception): _intro_mlp.stop()
        with contextlib.suppress(Exception): _intro_mlp.release()
    if _intro_ml:
        with contextlib.suppress(Exception): _intro_ml.release()
    _intro_mlp = None
    _intro_ml  = None

def play_intro_loop(lola_dir, zero_filename):
    """Fresh MediaListPlayer for 0.mkv every time."""
    global _intro_mlp, _intro_ml, _current_media_name
    if inst is None or player is None:
        raise RuntimeError("Video not initialized")

    detach_end_evt()
    with contextlib.suppress(Exception): player.stop()
    with contextlib.suppress(Exception): player.set_media(None)
    with contextlib.suppress(Exception): player.set_xwindow(_main_xid)

    stop_intro_loop()

    v0 = os.path.join(lola_dir, zero_filename)
    _intro_mlp = inst.media_list_player_new()
    _intro_mlp.set_media_player(player)
    _intro_ml = inst.media_list_new([v0])
    _intro_mlp.set_media_list(_intro_ml)
    _intro_mlp.set_playback_mode(vlc.PlaybackMode.loop)

    print("[PLAY] 0.mkv via fresh MediaListPlayer", flush=True)
    _intro_mlp.play()

    _current_media_name = zero_filename

def play_video(lola_dir, filename):
    """Play a single, non-looping video (only 0.mkv loops via MediaListPlayer)."""
    global _current_media_name
    if inst is None or player is None:
        raise RuntimeError("Video not initialized")

    vpath = os.path.join(lola_dir, filename)
    if not os.path.exists(vpath):
        print(f"[ERR] Video not found: {vpath}", file=sys.stderr)
        with contextlib.suppress(Exception): player.stop()
        with contextlib.suppress(Exception): player.set_media(None)
        return

    stop_intro_loop()

    detach_end_evt()
    with contextlib.suppress(Exception): player.stop()
    with contextlib.suppress(Exception): player.set_media(None)
    with contextlib.suppress(Exception): player.set_xwindow(_main_xid)

    m = inst.media_new(vpath)
    player.set_media(m)
    print(f"[PLAY] normal {filename}", flush=True)
    player.play()

    _attach_end_evt()
    _current_media_name = filename

# ===== Skip / Tail helpers =====
def _wait_for_length(timeout_s=2.0):
    t0 = time.time()
    L = player.get_length() or 0
    while L <= 0 and (time.time() - t0) < timeout_s:
        time.sleep(0.02)
        L = player.get_length() or 0
    return max(0, L)

def _approx_frame_ms():
    with contextlib.suppress(Exception):
        fps = float(player.video_get_fps() or 0.0)
        if fps and fps > 1.0:
            return int(min(100, max(10, round(1000.0 / fps))))
    return 33

def _set_blackout(enable: bool):
    """Temporarily black out video so any tail maneuver is invisible."""
    try:
        player.video_set_adjust_int(vlc.VideoAdjustOption.Enable, 1 if enable else 0)
        player.video_set_adjust_float(vlc.VideoAdjustOption.Brightness, 0.0 if enable else 1.0)
    except Exception:
        pass

def _force_jump_to_tail():
    """
    Aggressive, verified jump near the tail that works even on builds
    where a single seek is ignored. Keeps playback visible-black.
    """
    _set_blackout(True)
    try:
        with contextlib.suppress(Exception): player.set_rate(1.0)
        with contextlib.suppress(Exception): player.set_pause(False)

        L = _wait_for_length(1.0)
        frame_ms = _approx_frame_ms()
        safety_ms = max(120, 3 * frame_ms)
        targets_ms = [max(0, L - s) for s in (safety_ms, safety_ms + 80, safety_ms + 160)]
        pos_targets = [0.98, 0.99, 0.995, 0.998, 0.999, 0.9995]

        orig_t = player.get_time() or 0

        def _moved():
            cur = player.get_time() or 0
            return cur > orig_t + max(40, frame_ms)

        for t in targets_ms:
            with contextlib.suppress(Exception): player.set_time(t)
            time.sleep(0.06)
            if _moved():
                break
        else:
            for p in pos_targets:
                with contextlib.suppress(Exception): player.set_position(p)
                time.sleep(0.06)
                if _moved():
                    break
            else:
                with contextlib.suppress(Exception): player.set_rate(16.0)
                deadline = time.time() + 0.25
                while time.time() < deadline and not _moved():
                    time.sleep(0.01)
                with contextlib.suppress(Exception): player.set_rate(1.0)
                if L > 0:
                    with contextlib.suppress(Exception): player.set_time(max(0, L - safety_ms))
                else:
                    with contextlib.suppress(Exception): player.set_position(0.999)
                time.sleep(0.06)

        with contextlib.suppress(Exception): player.set_pause(True)
        time.sleep(0.01)

        last_t = player.get_time() or 0
        for _ in range(16):
            with contextlib.suppress(Exception): player.next_frame()
            time.sleep(0.012)
            cur_t = player.get_time() or 0
            if cur_t <= last_t:
                break
            last_t = cur_t

        with contextlib.suppress(Exception): player.set_rate(1.0)

    finally:
        _set_blackout(False)

def jump_to_tail_and_pause():
    """Public helper used by main when skipping: jump to tail & pause."""
    if player is None:
        return
    _force_jump_to_tail()

def shutdown_video():
    """Cleanly shut down VLC / video resources."""
    with contextlib.suppress(Exception): stop_intro_loop()
    detach_end_evt()
    if player is not None:
        with contextlib.suppress(Exception): player.stop()
        with contextlib.suppress(Exception): player.set_media(None)
        with contextlib.suppress(Exception): player.release()
    if inst is not None:
        with contextlib.suppress(Exception): inst.release()
