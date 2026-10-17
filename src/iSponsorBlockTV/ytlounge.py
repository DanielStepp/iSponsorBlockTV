import asyncio
import json
import sys
import time
from typing import Any
from uuid import uuid4

import pyytlounge
from aiohttp import ClientSession
from pyytlounge.event_listener import EventListener
from pyytlounge.events import NowPlayingEvent, PlaybackStateEvent
from pyytlounge.models import State
from pyytlounge.wrapper import Dict, NotConnectedException, NotLinkedException, api_base, as_aiter

from .constants import youtube_client_blacklist

create_task = asyncio.create_task


class PlaybackState:
    def __init__(self):
        self.currentTime = 0.0
        self.duration = 0.0
        self.videoId = ""
        self.state = State.Stopped


class _CallbackListener(EventListener):
    def __init__(self, lounge: "YtLoungeApi"):
        super().__init__()
        self._lounge = lounge

    async def playback_state_changed(self, event: PlaybackStateEvent) -> None:
        await self._lounge._handle_playback_state_event(event)

    async def now_playing_changed(self, event: NowPlayingEvent) -> None:
        await self._lounge._handle_now_playing_event(event)


class YtLoungeApi(pyytlounge.YtLoungeApi):
    # An ad replaces the content player, so its duration is normally much
    # shorter than the duration previously reported for the content video.
    _AD_DURATION_RATIO = 0.5
    # After replaying the content, the TV may start at a stale position. We seek to the
    # wanted position and verify it with the next playing event.
    _SEEK_TOLERANCE_BEFORE = 1.5
    _SEEK_TOLERANCE_AFTER = 8.0
    _SEEK_MAX_ATTEMPTS = 3
    _SEEK_RETRY_INTERVAL = 1.0
    # Never stay muted longer than this while waiting for the seek to be confirmed.
    # 0 disables the wait: unmute as soon as the content is replayed.
    _SEEK_UNMUTE_TIMEOUT = 1.5

    def __init__(
        self,
        screen_id=None,
        config=None,
        api_helper=None,
        logger=None,
    ):
        self._callback_listener = _CallbackListener(self)
        super().__init__(
            config.join_name if config else "iSponsorBlockTV",
            event_listener=self._callback_listener,
            logger=logger,
        )
        self.auth.screen_id = screen_id
        self.auth.lounge_id_token = None
        self.api_helper = api_helper
        self.volume_state = {}
        self.playback_speed = 1.0
        self.subscribe_task = None
        self.subscribe_task_watchdog = None
        self.callback = None
        self._state_callback = None
        self._playback_state = PlaybackState()
        self.logger = logger
        self.shorts_disconnected = False
        self.auto_play = True
        self.watchdog_running = False
        self.last_event_time = 0
        self._ad_active = False
        self._ad_started_position: float | None = None
        self._ad_dance_task = None
        self._pending_seek: tuple[str, float] | None = None
        self._seek_attempts = 0
        self._last_seek_time = 0.0
        self._last_seek_while_playing = False
        self._unmute_deferred = False
        # True while the last mute command we sent was a mute (so an unmute is due).
        self._muted_by_us = False
        self._unmute_timeout_task: asyncio.Task | None = None
        self._last_video_id = ""
        self._last_content_duration = 0.0
        self._last_playback_position = 0.0
        self._last_content_playback_speed = 1.0
        self._last_playback_info_time = time.monotonic()
        self._has_content_playback_info = False
        self.mute_ads = False
        self.skip_ads = False
        if config:
            self.mute_ads = config.mute_ads
            self.skip_ads = config.skip_ads
            self.auto_play = config.auto_play
        self._command_mutex = asyncio.Lock()
        self._command_message_offset = 1

    @staticmethod
    def _to_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _looks_like_ad_duration(self, duration: Any) -> bool:
        try:
            duration = float(duration)
        except (TypeError, ValueError):
            return False
        return (
            duration > 0
            and self._last_content_duration > 0
            and duration < self._last_content_duration * self._AD_DURATION_RATIO
        )

    def _set_ad_active(self) -> None:
        """Mark an ad as active and remember where the content was when it started.

        The ad is usually detected a moment after it started, and the content does not
        advance during the ad, so the position has to be frozen at the first signal.
        """
        if not self._ad_active:
            self._ad_started_position = self._position_before_ad()
            self.logger.debug("Ad detected, content position %.2f", self._ad_started_position)
        self._ad_active = True

    def _mute_for_ad(self, reason: str = "") -> None:
        """Mute as soon as any event reliably signals an ad.

        Without override this is a no-op when the device is already muted, so it is
        cheap to call from every early ad signal (nowPlaying, onStateChange, ...).
        The duration heuristic is deliberately not used here, since a false positive
        would mute real content.
        """
        if self.mute_ads:
            self.logger.info("Early ad signal (%s), muting", reason)
            create_task(self.mute(True))

    def _visible_state(self, state: State) -> State:
        """The state reported to listeners: an active ad hides the content state."""
        return State.Advertisement if self._ad_active else state

    def _remember_content_playback(self, position: float, duration: float | None) -> None:
        """Remember where the content is, to restore it after a non-skippable ad."""
        self._last_playback_position = position
        self._last_content_playback_speed = self.playback_speed
        self._last_playback_info_time = time.monotonic()
        self._has_content_playback_info = True
        if duration is not None and duration > 0:
            self._last_content_duration = duration

    async def _handle_playback_state_event(self, event: PlaybackStateEvent) -> None:
        self._playback_state.currentTime = event.current_time
        self._playback_state.duration = event.duration
        self._playback_state.state = self._visible_state(event.state)
        if not self._ad_active and event.state is State.Playing:
            self._remember_content_playback(event.current_time, event.duration)
        await self._seek_when_content_ready(event.state, event.current_time)
        if self._state_callback:
            await self._state_callback(self._playback_state)

    async def _handle_now_playing_event(self, event: NowPlayingEvent) -> None:
        if event.current_time is not None:
            self._playback_state.currentTime = event.current_time
        if event.duration is not None:
            self._playback_state.duration = event.duration
        if event.video_id and event.video_id != self._last_video_id:
            if self._pending_seek and event.video_id != self._pending_seek[0]:
                self._clear_pending_seek()
            self._last_video_id = event.video_id
            self._last_content_duration = 0.0
            self._last_playback_position = 0.0
            self._has_content_playback_info = False
        self._playback_state.videoId = self._last_video_id
        self._playback_state.state = self._visible_state(event.state)
        if (
            not self._ad_active
            and event.current_time is not None
            and event.state is State.Playing
        ):
            self._remember_content_playback(event.current_time, event.duration)
        await self._seek_when_content_ready(event.state, event.current_time)
        if self._state_callback:
            await self._state_callback(self._playback_state)

    def _set_pending_seek(self, pending: tuple[str, float]) -> None:
        self._pending_seek = pending
        self._seek_attempts = 0
        self._last_seek_time = 0.0
        self._last_seek_while_playing = False

    def _clear_pending_seek(self) -> None:
        self._pending_seek = None
        self._flush_deferred_unmute()

    def _flush_deferred_unmute(self) -> None:
        if self._unmute_deferred:
            self._unmute_deferred = False
            if self.mute_ads:
                self.logger.info("Ad has ended, unmuting")
                create_task(self.mute(False))

    def _unmute_after_ad(self) -> None:
        """Unmute after an ad, but not while a seek to the right position is pending.

        Otherwise the stale position the TV starts at after replaying the content is
        audible until the seek has been applied.
        """
        if not self.mute_ads:
            return
        if self._pending_seek and self._SEEK_UNMUTE_TIMEOUT > 0:
            self._unmute_deferred = True
            if self._unmute_timeout_task and not self._unmute_timeout_task.done():
                self._unmute_timeout_task.cancel()
            self._unmute_timeout_task = create_task(self._unmute_timeout())
            return
        self.logger.info("Ad has ended, unmuting")
        create_task(self.mute(False))

    async def _unmute_timeout(self) -> None:
        try:
            await asyncio.sleep(self._SEEK_UNMUTE_TIMEOUT)
        except asyncio.CancelledError:
            return
        if self._unmute_deferred:
            self.logger.debug("Seek not confirmed in time, unmuting anyway")
            self._pending_seek = None
            self._flush_deferred_unmute()

    async def _send_seek(self, position: float) -> None:
        try:
            await self.seek_to(position)
        except Exception:
            self.logger.exception("Seek to %.2f seconds failed", position)

    async def _seek_when_content_ready(
        self, state: State, current_time: float | None = None
    ) -> None:
        """Seek to the pending position and verify it with the next playing event."""
        if self._ad_active or not self._pending_seek:
            return
        video_id, position = self._pending_seek
        if self._last_video_id != video_id:
            return
        # Seeking while the TV is still loading (Starting) already works and shortens the
        # time the stale position is visible.
        if state not in (State.Playing, State.Starting):
            return
        if (
            current_time is not None
            and position - self._SEEK_TOLERANCE_BEFORE
            <= current_time
            <= position + self._SEEK_TOLERANCE_AFTER
        ):
            self.logger.debug(
                "Video %s is at %.2f seconds (wanted %.2f), no seek needed",
                video_id,
                current_time,
                position,
            )
            self._clear_pending_seek()
            return
        if self._seek_attempts >= self._SEEK_MAX_ATTEMPTS:
            self.logger.debug("Giving up seeking video %s to %.2f seconds", video_id, position)
            self._clear_pending_seek()
            return
        now = time.monotonic()
        # A seek sent while the TV was still loading may have been ignored, so retry
        # right away once it plays at the wrong position. Otherwise wait a bit so
        # repeated events don't trigger a seek each.
        retry_now = state is State.Playing and not self._last_seek_while_playing
        if self._seek_attempts and not retry_now:
            if now - self._last_seek_time < self._SEEK_RETRY_INTERVAL:
                return
        elif self._seek_attempts and now - self._last_seek_time < 0.2:
            return
        self._seek_attempts += 1
        self._last_seek_time = now
        self._last_seek_while_playing = state is State.Playing
        self.logger.debug(
            "Content ready (%s), seeking video %s to %.2f seconds (attempt %d)",
            state.name,
            video_id,
            position,
            self._seek_attempts,
        )
        create_task(self._send_seek(position))

    def _position_before_ad(self) -> float:
        """Estimate the content position when the ad started."""
        if self._ad_active and self._ad_started_position is not None:
            return self._ad_started_position
        if not self._has_content_playback_info:
            return 0.0
        elapsed = time.monotonic() - self._last_playback_info_time
        return max(
            0.0,
            self._last_playback_position
            + elapsed * self._last_content_playback_speed,
        )

    async def _replay_video_after_ad(
        self, video_id: str, position: float, wait_time: float
    ) -> None:
        try:
            await asyncio.sleep(wait_time)
            self.logger.info(
                "Replaying non-skippable ad content %s from %.2f seconds",
                video_id,
                position,
            )
            if position != 0:
                await self.play_video_and_force_seek(video_id, position)
            else:
                await self.play_video(video_id, position)
            # The ad is replaced by the content now. Unmute right away instead of
            # waiting for the next playback event. If a seek is pending, this waits
            # until the seek is confirmed.
            self._unmute_after_ad()
        except asyncio.CancelledError:
            pass

    # Ensures that we still are subscribed to the lounge
    async def _watchdog(self):
        """
        Continuous watchdog that monitors for connection health.
        If no events are received within the expected timeframe,
        it cancels the current subscription.
        """
        self.watchdog_running = True
        self.last_event_time = asyncio.get_running_loop().time()

        try:
            while self.watchdog_running:
                await asyncio.sleep(10)
                current_time = asyncio.get_running_loop().time()
                time_since_last_event = current_time - self.last_event_time

                # YouTube sends a message at least every 30 seconds
                if time_since_last_event > 60:
                    self.logger.debug(
                        f"Watchdog triggered: No events for {time_since_last_event:.1f} seconds"
                    )

                    # Cancel current subscription
                    if self.subscribe_task and not self.subscribe_task.done():
                        self.subscribe_task.cancel()
                        await asyncio.sleep(1)  # Give it time to cancel
        except asyncio.CancelledError:
            self.logger.debug("Watchdog task cancelled")
            self.watchdog_running = False
        except BaseException as e:
            self.logger.error(f"Watchdog error: {e}")
            self.watchdog_running = False

    # Subscribe to the lounge and start the watchdog
    async def subscribe_monitored(self, callback):
        self.callback = callback
        self._state_callback = callback

        # Stop existing watchdog if running
        if self.subscribe_task_watchdog and not self.subscribe_task_watchdog.done():
            self.watchdog_running = False
            self.subscribe_task_watchdog.cancel()
            try:
                await self.subscribe_task_watchdog
            except (asyncio.CancelledError, Exception):
                pass

        # Start new subscription
        if self.subscribe_task and not self.subscribe_task.done():
            self.subscribe_task.cancel()
            try:
                await self.subscribe_task
            except (asyncio.CancelledError, Exception):
                pass

        self.subscribe_task = asyncio.create_task(super().subscribe())
        self.subscribe_task_watchdog = asyncio.create_task(self._watchdog())
        return self.subscribe_task

    # Lounge events handled here (before pyytlounge sees them), event type -> method name.
    _EVENT_HANDLERS = {
        "onStateChange": "_on_state_change",
        "nowPlaying": "_on_now_playing",
        "onAdStateChange": "_on_ad_state_change",
        "adPlaying": "_on_ad_playing",
        "onVolumeChanged": "_on_volume_changed",
        "autoplayUpNext": "_on_autoplay_up_next",
        "loungeStatus": "_on_lounge_status",
        "onSubtitlesTrackChanged": "_on_subtitles_track_changed",
        "loungeScreenDisconnected": "_on_lounge_screen_disconnected",
        "onAutoplayModeChanged": "_on_autoplay_mode_changed",
        "onPlaybackSpeedChanged": "_on_playback_speed_changed",
    }

    # Process a lounge subscription event
    async def _process_event(self, event_type: str, args: list[Any]):
        self.logger.debug(f"process_event({event_type}, {args})")
        # Update last event time for the watchdog
        self.last_event_time = asyncio.get_running_loop().time()

        handler_name = self._EVENT_HANDLERS.get(event_type)
        if handler_name and getattr(self, handler_name)(args):
            return  # handler asked to drop the event

        await super()._process_event(event_type, args)

    # --- ad detection ---------------------------------------------------------------
    # onStateChange, nowPlaying and onAdStateChange are useful to detect ads playing,
    # and the next video before it starts playing (that way we can get the segments).

    def _on_state_change(self, args: list[Any]) -> None:
        data = args[0]
        if data.get("state") == str(State.Advertisement.value):
            self._set_ad_active()
            self._mute_for_ad("onStateChange")
        elif self._looks_like_ad_duration(data.get("duration")):
            self._set_ad_active()
        # Unmute when the video starts playing
        if not self._ad_active and data.get("state") == "1":
            self._unmute_after_ad()

    def _on_now_playing(self, args: list[Any]) -> None:
        data = args[0]
        if data.get("adState") == "1" or data.get("adVideoId"):
            self._set_ad_active()
            self._mute_for_ad("nowPlaying")
        elif self._looks_like_ad_duration(data.get("duration")):
            self._set_ad_active()
        elif data.get("videoId") and data.get("duration") not in (None, "0", 0):
            self._ad_active = False
        # Unmute when the video starts playing
        if not self._ad_active and data.get("state", "0") == "1":
            self._unmute_after_ad()

    def _on_ad_state_change(self, args: list[Any]) -> None:
        data = args[0]
        if data["adState"] == "0":  # Ad is not playing
            self._ad_active = False
            self._cancel_ad_dance()
            if data.get("currentTime") != "0":
                self._unmute_after_ad()
        elif self._can_skip_ad(data):
            self._skip_ad_now()
        elif self.mute_ads:  # Seen multiple other adStates, assuming they are all ads
            self._mute_ad_started()

    # Used to know if an ad is skippable or not
    def _on_ad_playing(self, args: list[Any]) -> None:
        data = args[0]
        self._set_ad_active()
        # Gets segments for the next video (after the ad) before it starts playing
        if self._last_video_id:
            self.logger.info(f"Getting segments for next video: {self._last_video_id}")
            create_task(self.api_helper.get_segments(self._last_video_id))

        if self._can_skip_ad(data):
            self._skip_ad_now()
        elif self.skip_ads:  # ad is not skippable -> replay content after ~5s
            self._schedule_ad_dance(data)
            if self.mute_ads:
                create_task(self.mute(True))
        elif self.mute_ads:  # Seen multiple other adStates, assuming they are all ads
            self._mute_ad_started()

    def _can_skip_ad(self, data: dict[str, Any]) -> bool:
        # YouTube uses strings for booleans
        return bool(self.skip_ads) and data.get("isSkipEnabled") == "true"

    def _skip_ad_now(self) -> None:
        self.logger.info("Ad can be skipped, skipping")
        create_task(self.skip_ad())
        create_task(self.mute(False))

    def _mute_ad_started(self) -> None:
        self.logger.info("Ad has started, muting")
        create_task(self.mute(True))

    def _cancel_ad_dance(self) -> None:
        if self._ad_dance_task and not self._ad_dance_task.done():
            self._ad_dance_task.cancel()

    def _schedule_ad_dance(self, data: dict[str, Any]) -> None:
        """Replay the content from where it was once the ad has been playing ~5 seconds."""
        self._cancel_ad_dance()
        if not self._last_video_id:
            return
        self.logger.info("Ad cannot be skipped, scheduling ad dance")
        wait_time = max(0.0, 5.0 - self._to_float(data.get("currentTime")))
        self._ad_dance_task = create_task(
            self._replay_video_after_ad(
                self._last_video_id, self._position_before_ad(), wait_time
            )
        )

    # --- other lounge events ---------------------------------------------------------

    # Manages volume, useful since YouTube wants to know the volume
    # when unmuting (even if they already have it)
    def _on_volume_changed(self, args: list[Any]) -> None:
        self.volume_state = args[0]

    # Gets segments for the next video before it starts playing
    def _on_autoplay_up_next(self, args: list[Any]) -> None:
        if len(args) > 0 and (vid_id := args[0]["videoId"]):  # if video id is not empty
            self.logger.info(f"Getting segments for next video: {vid_id}")
            create_task(self.api_helper.get_segments(vid_id))

    def _on_lounge_status(self, args: list[Any]) -> bool:
        """Returns True (and forces a disconnect) when a blacklisted client is connected."""
        devices = json.loads(args[0]["devices"])
        for device in devices:
            if device["type"] == "LOUNGE_SCREEN":
                device_info = json.loads(device.get("deviceInfo", "{}"))
                if device_info.get("clientName", "") in youtube_client_blacklist:
                    self._sid = None
                    self._gsession = None  # Force disconnect
                    return True
        return False

    def _on_subtitles_track_changed(self, args: list[Any]) -> None:
        if self.shorts_disconnected:
            video_id_saved = args[0].get("videoId", None)
            self.shorts_disconnected = False
            create_task(self.play_video(video_id_saved))

    def _on_lounge_screen_disconnected(self, args: list[Any]) -> None:
        if args:  # Sometimes it's empty
            if args[0]["reason"] == "disconnectedByUserScreenInitiated":  # Short playing?
                self.shorts_disconnected = True

    def _on_autoplay_mode_changed(self, args: list[Any]) -> None:
        create_task(self.set_auto_play_mode(self.auto_play))

    def _on_playback_speed_changed(self, args: list[Any]) -> None:
        self.playback_speed = float(args[0].get("playbackSpeed", "1"))

    # Set the volume to a specific value (0-100)
    async def set_volume(self, volume: int) -> None:
        await self._command("setVolume", {"volume": volume})

    async def mute(self, mute: bool, override: bool = False) -> None:
        """
        Mute or unmute the device.

        Every command is a request to YouTube and commands are sent one after another,
        so redundant ones delay the important ones. The command is therefore skipped
        when the device is already known to be in the desired state. After we muted the
        device, an unmute is always sent.

        :param bool mute: True to mute, False to unmute
        :param bool override: If True, the command is sent even if the
        device already is in the desired state

        TODO: Only works if the device is subscribed to the lounge
        """
        mute_str = "true" if mute else "false"
        needed = self.volume_state.get("muted") != mute_str or (not mute and self._muted_by_us)
        if not (override or needed):
            return
        had_state = "muted" in self.volume_state
        previous_state = self.volume_state.get("muted")
        previous_flag = self._muted_by_us
        self.volume_state["muted"] = mute_str
        self._muted_by_us = mute

        def revert() -> None:
            # Don't believe in a state the device never got
            if had_state:
                self.volume_state["muted"] = previous_state
            else:
                self.volume_state.pop("muted", None)
            self._muted_by_us = previous_flag

        started = time.monotonic()
        try:
            # YouTube wants the volume when unmuting, so we send it
            success = await self._command(
                "setVolume",
                {"volume": self.volume_state.get("volume", 100), "muted": mute_str},
            )
        except BaseException:
            revert()
            raise
        if not success:
            revert()
        else:
            # Includes the time spent waiting for earlier commands to finish
            self.logger.info(
                "%s command done after %.0f ms", "Mute" if mute else "Unmute",
                (time.monotonic() - started) * 1000,
            )

    async def play_video(self, video_id: str, current_time: float | None = None) -> bool:
        self._clear_pending_seek()
        parameters: dict[str, Any] = {"videoId": video_id}
        if current_time is not None:
            parameters["currentTime"] = current_time
            self.logger.debug("Playing video %s from %s seconds", video_id, current_time)
        return await self._command("setPlaylist", parameters)

    async def play_video_and_force_seek(self, video_id: str, current_time: float) -> bool:
        """Load a video, then seek once its content reports playing."""
        if current_time == 0:
            self.logger.debug(
                "Force seek to 0 seconds for video %s, sending only setPlaylist", video_id
            )
            return await self.play_video(video_id, current_time)
        self.logger.debug(
            "Waiting for video %s to play before seeking to %.2f seconds", video_id, current_time
        )
        pending = (video_id, current_time)
        self._set_pending_seek(pending)
        try:
            success = await self._command(
                "setPlaylist", {"videoId": video_id, "currentTime": current_time}
            )
        except BaseException:
            if self._pending_seek is pending:
                self._clear_pending_seek()
            raise
        if not success and self._pending_seek is pending:
            self._clear_pending_seek()
        return success

    async def get_now_playing(self):
        return await self._command("getNowPlaying")

    async def _command(
        self, command: str, command_parameters: dict[str, Any] | None = None
    ) -> bool:
        return await self._commands([(command, command_parameters)])

    async def _commands(
        self, commands: list[tuple[str, dict[str, Any] | None]]
    ) -> bool:
        """Send ordered commands, counting requests and command messages separately."""
        if not commands:
            raise ValueError("At least one command is required")
        async with self._command_mutex:
            if not self.connected():
                raise NotConnectedException("Not connected")

            command_body: dict[str, Any] = {
                "count": len(commands),
                "ofs": self._command_message_offset,
            }
            for index, (command, parameters) in enumerate(commands):
                command_body[f"req{index}__sc"] = command
                for name, value in (parameters or {}).items():
                    command_body[f"req{index}_{name}"] = value

            self._command_message_offset += len(commands)
            # RID counts HTTP requests; ofs counts the messages inside them.
            self._command_offset += 1
            params = {
                **self._common_connection_parameters(),
                "RID": self._command_offset,
            }
            async with self._required_session.post(
                url=f"{api_base}/bc/bind", data=command_body, params=params
            ) as response:
                try:
                    response_text = await response.text()
                    if not self._handle_session_result(response.status, response_text):
                        return False
                    response.raise_for_status()
                    return True
                except Exception:
                    self._logger.exception("Command failed")
                    raise

    async def change_web_session(self, web_session: ClientSession):
        if self.session is not None:
            await self.session.close()
        if self.conn is not None:
            await self.conn.close()
        self.session = web_session

    def _common_connection_parameters(self) -> Dict[str, Any]:
        return {
            "name": self.device_name,
            "loungeIdToken": self.auth.lounge_id_token,
            "SID": self._sid,
            "AID": self._last_event_id,
            "gsessionid": self._gsession,
            "device": "REMOTE_CONTROL",
            "app": "ytios-phone-20.15.1",
            "VER": "8",
            "v": "2",
        }

    async def connect(self) -> bool:
        """Attempt to connect using the previously set tokens"""
        self._clear_pending_seek()
        if not self.linked():
            raise NotLinkedException("Not linked")

        connect_body = {
            "id": self.auth.screen_id,
            "mdx-version": "3",
            "TYPE": "xmlhttp",
            "theme": "cl",
            "sessionSource": "MDX_SESSION_SOURCE_UNKNOWN",
            "connectParams": '{"setStatesParams": "{"playbackSpeed":0}"}',
            "RID": "1",
            "CVER": "1",
            "capabilities": "que,dsdtr,atp,vsp",
            "ui": "false",
            "app": "ytios-phone-20.15.1",
            "pairing_type": "manual",
            "VER": "8",
            "loungeIdToken": self.auth.lounge_id_token,
            "device": "REMOTE_CONTROL",
            "name": self.device_name,
        }
        connect_url = f"{api_base}/bc/bind"
        async with self.session.post(url=connect_url, data=connect_body) as resp:
            try:
                text = await resp.text()
                if resp.status == 401:
                    if "Connection denied" in text:
                        self._logger.warning(
                            "Connection denied, attempting to circumvent the issue"
                        )
                        await self.connect_as_screen()
                    # self._lounge_token_expired()
                    return False

                if resp.status != 200:
                    self._logger.warning("Unknown reply to connect %i %s", resp.status, resp.reason)
                    return False
                lines = text.splitlines()
                async for events in self._parse_event_chunks(as_aiter(lines)):
                    await self._process_events(events)
                self._command_offset = 1
                self._command_message_offset = 1
                return self.connected()
            except Exception:
                self._logger.exception(
                    "Handle connect failed, status %s reason %s",
                    resp.status,
                    resp.reason,
                )
                raise

    async def connect_as_screen(self) -> bool:
        """Attempt to connect using the previously set tokens"""
        if not self.linked():
            raise NotLinkedException("Not linked")

        connect_body = {
            "id": str(uuid4()),
            "mdx-version": "3",
            "TYPE": "xmlhttp",
            "theme": "cl",
            "sessionSource": "MDX_SESSION_SOURCE_UNKNOWN",
            "connectParams": '{"setStatesParams": "{"playbackSpeed":0}"}',
            "sessionNonce": str(uuid4()),
            "RID": "1",
            "CVER": "1",
            "capabilities": "que,dsdtr,atp,vsp",
            "ui": "false",
            "app": "ytios-phone-20.15.1",
            "pairing_type": "manual",
            "VER": "8",
            "loungeIdToken": self.auth.lounge_id_token,
            "device": "LOUNGE_SCREEN",
            "name": self.device_name,
        }
        connect_url = f"{api_base}/bc/bind"
        async with self.session.post(url=connect_url, data=connect_body) as resp:
            try:
                await resp.text()
                self.logger.error(
                    "Connected as screen: please force close the app on the device "
                    "for iSponsorBlockTV to work properly"
                )
                self.logger.warning("Exiting in 5 seconds")
                await asyncio.sleep(5)
                sys.exit(0)
            except Exception:
                self._logger.exception(
                    "Handle connect failed, status %s reason %s",
                    resp.status,
                    resp.reason,
                )
                raise
