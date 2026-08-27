# Bugfix Requirements Document

## Introduction

Three related bugs in the unified playback queue's video transition logic prevent correct behavior when skipping between video entries, when audio entries follow video entries, and when a video session ends without a successor. Together, these break the video-to-video skip flow, allow audio to play alongside an active video, and leave the Activity on a blank screen instead of showing the DVD screensaver idle state.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN a music_video is playing in the Activity AND the user skips AND the next entry in the unified queue is also a music_video THEN the system starts audio playback for the next track while the old video continues playing visually in the Activity (the video session is not terminated and replaced)

1.2 WHEN a music_video is playing in the Activity AND the user skips AND the next entry in the unified queue is a regular audio track THEN the system starts audio playback for that track while the video session remains active (audio plays alongside the still-visible video)

1.3 WHEN a video session ends naturally (queue empty, streamer calls `_stop_internal`) AND no more music_video entries are queued THEN the Activity frontend receives a `session_end` message and transitions to IDLE mode (blank/stale screen) instead of showing the DVD bouncing logo screensaver

### Expected Behavior (Correct)

2.1 WHEN a music_video is playing in the Activity AND the user skips AND the next entry in the unified queue is also a music_video THEN the system SHALL terminate the current video session, broadcast a `session_change` to the Activity frontend, and start the new video entry in the Activity without any audio playback occurring

2.2 WHEN a music_video is playing in the Activity AND the user skips AND the next entry in the unified queue is a regular audio track THEN the system SHALL fully terminate the current video session first, then start audio playback only after the video session has been cleaned up (unregistered from the session registry)

2.3 WHEN a video session ends (either by skip with empty queue or natural playback completion) AND no successor video is queued THEN the Activity frontend SHALL transition to the VISUALIZER_DVD mode displaying the DVD bouncing logo screensaver (not IDLE/blank)

### Unchanged Behavior (Regression Prevention)

3.1 WHEN a music_video is playing AND the streamer's internal video queue has items THEN the system SHALL CONTINUE TO skip within the streamer's internal queue without involving the unified player queue

3.2 WHEN an audio track is playing (no video session active) AND the user skips THEN the system SHALL CONTINUE TO advance the unified queue normally via Lavalink without any video-related logic interfering

3.3 WHEN a video session is idle but has connected WebSocket clients AND a new music_video entry arrives in the unified queue THEN the system SHALL CONTINUE TO reuse the idle streamer session (play on existing streamer without launching a new Activity)

3.4 WHEN the unified queue is empty after both video and audio entries are exhausted THEN the system SHALL CONTINUE TO call `_on_queue_empty` which handles disconnect/idle behavior

3.5 WHEN a music_video entry is being set up (transition in progress) AND an audio track is next in the unified queue THEN the system SHALL CONTINUE TO block audio playback via the `_video_transition` flag until the video setup completes
