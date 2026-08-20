# Bugfix Requirements Document

## Introduction

The bot incorrectly leaves the voice channel when the command-issuing user disconnects, even when other humans remain in the channel. Additionally, the bot lacks an idle timeout for no-playback scenarios and does not play 808 bass sound effects on join/leave transitions. This fix addresses three related issues: premature voice departure, missing idle timeout, and missing join/leave audio cues.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN the user who invoked /play or /join leaves the voice channel AND other non-bot users remain in the channel THEN the system disconnects from voice prematurely

1.2 WHEN the bot is in a voice channel with no active playback AND humans are still present THEN the system stays indefinitely with no idle timeout mechanism

1.3 WHEN the bot joins a voice channel THEN the system does not play an 808 bass sound effect

1.4 WHEN the bot leaves a voice channel (for any reason) THEN the system does not play an 808 bass sound effect before disconnecting

### Expected Behavior (Correct)

2.1 WHEN any user leaves the voice channel AND at least one non-bot user remains in the channel THEN the system SHALL remain connected to the voice channel

2.2 WHEN all non-bot users have left the voice channel THEN the system SHALL disconnect after a short grace period (existing 10-second alone_task behavior)

2.3 WHEN the bot has no active playback for a configurable idle timeout period AND humans are still present in the channel THEN the system SHALL disconnect from voice and notify the text channel

2.4 WHEN the bot joins a voice channel THEN the system SHALL play the configured 808 bass chime sound effect upon connection

2.5 WHEN the bot is about to leave a voice channel (all-humans-left, idle timeout, or explicit /leave command) THEN the system SHALL play the configured 808 bass chime sound effect before disconnecting

### Unchanged Behavior (Regression Prevention)

3.1 WHEN all non-bot users leave the voice channel THEN the system SHALL CONTINUE TO use the existing alone_task with a 10-second grace period before disconnecting

3.2 WHEN a user issues /leave, /fuckoff, or /l THEN the system SHALL CONTINUE TO disconnect immediately (after playing the leave chime)

3.3 WHEN the sleep timeout fires due to nobody being in the channel THEN the system SHALL CONTINUE TO park/save the queue and disconnect

3.4 WHEN the bot is playing music and users are in the channel THEN the system SHALL CONTINUE TO remain connected and play without interruption

3.5 WHEN the bot has an active queue and all humans leave THEN the system SHALL CONTINUE TO save/park the queue for later /continue usage
