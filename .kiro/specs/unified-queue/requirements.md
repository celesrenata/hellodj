# Requirements Document

## Introduction

HelloDJ currently maintains two independent queue systems: an audio queue (managed by `player.py` guild state) dispatched to Lavalink via wavelink, and a video queue (managed by `ActivityStreamer`) dispatched to the Discord Activity HLS pipeline. These queues are unaware of each other, causing items queued in one system to play immediately even when the other system is actively playing — audio starts over video, or vice versa.

This feature replaces both independent queues with a single unified queue per channel session. The unified queue holds both audio items and video items in insertion order and plays them sequentially. When the current item finishes, the next item is dispatched to the appropriate backend (Lavalink for audio, Activity pipeline for video) regardless of type. All user-facing commands (`/play`, `/skip`, `/stop`, `/queue`, `/clear`, `/shuffle`) operate on this single queue.

## Glossary

- **Unified_Queue**: A single ordered list of QueueItems (audio and video) associated with a channel session, replacing the separate audio and video queues.
- **QueueItem**: A tagged entry in the Unified_Queue containing metadata (title, URL, requester, duration) and a dispatch type discriminator (audio or video).
- **Dispatch_Type**: An enum indicating whether a QueueItem should be routed to the Audio_Backend or the Video_Backend for playback.
- **Audio_Backend**: The Lavalink/wavelink playback engine that streams audio to the voice channel.
- **Video_Backend**: The Activity pipeline (HLS transcode → Discord Activity iframe) that streams video to the channel.
- **PlaybackRouter**: The central command dispatcher that routes user commands to the appropriate backend based on content classification and session state.
- **SessionRegistry**: The registry storing active ChannelSession objects keyed by (guild_id, channel_id).
- **ChannelSession**: A per-channel playback session that owns one Unified_Queue and tracks the currently playing item.
- **Advance_Logic**: The mechanism that detects when the current item finishes and dispatches the next item from the Unified_Queue.
- **ContentClassifier**: The module that determines whether user input resolves to audio or video content.

## Requirements

### Requirement 1: Unified Queue Data Structure

**User Story:** As a DJ, I want all my queued songs and videos held in one list, so that playback order is predictable regardless of content type.

#### Acceptance Criteria

1. THE Unified_Queue SHALL store QueueItems in insertion order as a single ordered sequence.
2. WHEN a QueueItem is added, THE Unified_Queue SHALL assign it a monotonically increasing position index.
3. THE QueueItem SHALL contain: title, URL, requester_id, duration_seconds, dispatch_type (audio or video), and source_metadata.
4. THE Unified_Queue SHALL accept QueueItems with dispatch_type "audio" and QueueItems with dispatch_type "video" interleaved in any order.
5. THE Unified_Queue SHALL enforce a maximum capacity of 200 items per channel session.
6. IF a QueueItem is added when the Unified_Queue has reached maximum capacity, THEN THE Unified_Queue SHALL reject the addition and return a capacity error.

### Requirement 2: Sequential Playback Across Types

**User Story:** As a listener, I want videos and songs to play one after another without overlap, so that I hear and see content in the order it was queued.

#### Acceptance Criteria

1. WHILE an audio QueueItem is playing via the Audio_Backend, THE Advance_Logic SHALL wait for the Audio_Backend to signal track completion before dispatching the next QueueItem.
2. WHILE a video QueueItem is playing via the Video_Backend, THE Advance_Logic SHALL wait for the Video_Backend to signal video completion before dispatching the next QueueItem.
3. WHEN the current QueueItem finishes, THE Advance_Logic SHALL dequeue the next QueueItem and dispatch it to the backend matching its dispatch_type.
4. WHEN a transition from an audio item to a video item occurs, THE Advance_Logic SHALL stop the Audio_Backend before starting the Video_Backend.
5. WHEN a transition from a video item to an audio item occurs, THE Advance_Logic SHALL stop the Video_Backend before starting the Audio_Backend.
6. THE Advance_Logic SHALL ensure only one backend is actively playing at any given time within a single channel session.

### Requirement 3: Audio Dispatch

**User Story:** As a DJ, I want audio items dispatched to Lavalink exactly as they are today, so that audio quality and source resolution are unchanged.

#### Acceptance Criteria

1. WHEN the Advance_Logic dispatches an audio QueueItem, THE Audio_Backend SHALL resolve the track via the existing wavelink search and source resolution pipeline.
2. WHEN the Audio_Backend begins playback, THE ChannelSession SHALL update its current item to the dispatched QueueItem.
3. WHEN the Audio_Backend emits a track-end event, THE Advance_Logic SHALL treat the audio QueueItem as complete.
4. IF the Audio_Backend fails to resolve or play a track, THEN THE Advance_Logic SHALL skip the failed QueueItem and dispatch the next item in the Unified_Queue.

### Requirement 4: Video Dispatch

**User Story:** As a DJ, I want video items dispatched to the Activity pipeline exactly as they are today, so that HLS streaming and the Activity iframe continue to work.

#### Acceptance Criteria

1. WHEN the Advance_Logic dispatches a video QueueItem, THE Video_Backend SHALL launch or reuse the Activity session and begin HLS transcoding for the video source.
2. WHEN the Video_Backend begins playback, THE ChannelSession SHALL update its current item to the dispatched QueueItem.
3. WHEN the Video_Backend signals video completion (stream ended), THE Advance_Logic SHALL treat the video QueueItem as complete.
4. IF the Video_Backend fails to transcode or stream a video, THEN THE Advance_Logic SHALL skip the failed QueueItem and dispatch the next item in the Unified_Queue.

### Requirement 5: Play Command Integration

**User Story:** As a user, I want `/play` to add items to the unified queue regardless of whether they are audio or video, so that I use one command for everything.

#### Acceptance Criteria

1. WHEN a user issues `/play` with a query, THE ContentClassifier SHALL determine the dispatch_type (audio or video).
2. WHEN content is classified as audio, THE PlaybackRouter SHALL create a QueueItem with dispatch_type "audio" and append it to the Unified_Queue.
3. WHEN content is classified as video, THE PlaybackRouter SHALL create a QueueItem with dispatch_type "video" and append it to the Unified_Queue.
4. WHEN the Unified_Queue is empty and no item is currently playing, THE PlaybackRouter SHALL dispatch the newly added QueueItem immediately.
5. WHEN the Unified_Queue has items or an item is currently playing, THE PlaybackRouter SHALL append the QueueItem and confirm the queue position to the user.

### Requirement 6: Skip Command

**User Story:** As a user, I want `/skip` to stop whatever is currently playing (audio or video) and advance to the next item in the unified queue.

#### Acceptance Criteria

1. WHEN a user issues `/skip`, THE PlaybackRouter SHALL stop the currently playing item on its respective backend.
2. WHEN the current item is stopped by skip, THE Advance_Logic SHALL dispatch the next QueueItem from the Unified_Queue.
3. IF no items remain in the Unified_Queue after a skip, THEN THE PlaybackRouter SHALL report that the queue is empty and enter an idle state.

### Requirement 7: Stop Command

**User Story:** As a user, I want `/stop` to halt all playback and clear the entire unified queue.

#### Acceptance Criteria

1. WHEN a user issues `/stop`, THE PlaybackRouter SHALL stop the currently playing backend (Audio_Backend or Video_Backend).
2. WHEN a user issues `/stop`, THE Unified_Queue SHALL be cleared of all remaining QueueItems.
3. WHEN a user issues `/stop`, THE ChannelSession SHALL transition to idle state with no current item.

### Requirement 8: Queue Display Command

**User Story:** As a user, I want `/queue` to show all upcoming items (audio and video) in one list with type indicators, so that I can see what's coming up.

#### Acceptance Criteria

1. WHEN a user issues `/queue`, THE PlaybackRouter SHALL display the currently playing item with its dispatch_type indicator (🎵 for audio, 🎬 for video).
2. WHEN a user issues `/queue`, THE PlaybackRouter SHALL list all remaining QueueItems in order with their position, title, requester, and dispatch_type indicator.
3. WHEN the Unified_Queue contains more than 10 items, THE PlaybackRouter SHALL paginate the display.

### Requirement 9: Clear Command

**User Story:** As a user, I want `/clear` to remove all upcoming items from the unified queue without stopping the currently playing item.

#### Acceptance Criteria

1. WHEN a user issues `/clear`, THE Unified_Queue SHALL remove all items except the currently playing item.
2. WHEN a user issues `/clear`, THE PlaybackRouter SHALL confirm the number of items removed.
3. THE currently playing item SHALL continue playback uninterrupted after a clear operation.

### Requirement 10: Shuffle Command

**User Story:** As a user, I want `/shuffle` to randomize the order of upcoming items in the unified queue.

#### Acceptance Criteria

1. WHEN a user issues `/shuffle`, THE Unified_Queue SHALL randomize the order of all items except the currently playing item.
2. WHEN a user issues `/shuffle`, THE PlaybackRouter SHALL confirm the queue has been shuffled.
3. THE currently playing item SHALL continue playback uninterrupted after a shuffle operation.

### Requirement 11: Backend Exclusivity

**User Story:** As a listener, I want audio and video to never play simultaneously in the same channel, so that sound does not overlap.

#### Acceptance Criteria

1. THE ChannelSession SHALL maintain at most one active backend (Audio_Backend or Video_Backend) at any time.
2. WHEN dispatching a QueueItem whose dispatch_type differs from the currently active backend, THE Advance_Logic SHALL fully stop the current backend before starting the new one.
3. IF a stop signal to the current backend times out after 10 seconds, THEN THE Advance_Logic SHALL force-terminate the backend and proceed with the next dispatch.

### Requirement 12: Session Lifecycle

**User Story:** As a user, I want the unified queue to persist across type transitions within a session, so that switching from a video to a song does not lose my queued items.

#### Acceptance Criteria

1. WHILE a ChannelSession is active, THE Unified_Queue SHALL persist its contents regardless of backend transitions.
2. WHEN all items in the Unified_Queue have been played and no repeat mode is active, THE ChannelSession SHALL enter idle state.
3. WHEN a ChannelSession enters idle state after queue exhaustion, THE ChannelSession SHALL remain registered for 5 minutes before auto-cleanup.
4. IF the bot disconnects and reconnects within the grace period, THEN THE ChannelSession SHALL restore the Unified_Queue state and resume from the last known position.
