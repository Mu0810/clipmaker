# Requirements: AI Autopilot — make, edit and auto-publish clips to YouTube and Instagram

Spec `i-want-to-build-ai` · feature · approved 7 Oct 2026 · amended during design: 9.2, 10.6 · added after the usability review: 1.10, 10.10 (see Revision notes)

## Introduction

Clip Maker already turns a long video into captioned vertical clips with AI-written titles, and lets the user upload or schedule each clip by hand. This feature closes the loop. The user hands over a video; the AI chooses the clips, edits them, writes separate metadata for YouTube and Instagram, plans when each post goes out, and publishes it through the platforms' official APIs. A review queue and a clip editor let the user check and correct anything before it goes live, and safeguards stop unattended posting from duplicating, bursting or publishing risky content.

### What exists today (read from the code, 7 Oct 2026)

- **Works end to end:** YouTube-URL or file intake; transcript from YouTube captions or Whisper (English only); clips picked by a local Ollama model or a keyword heuristic (the configured Kimchi cloud engine is not used for picking); virality scores, titles, descriptions, tags, hashtags and an on-screen hook from Kimchi with fallbacks; silence trimming; a 9:16 blurred-background vertical with burned captions, hook and ducked music; a 16:9 thumbnail; manual and timed uploads to YouTube (Data API, or YouTube Studio browser automation) and Instagram (instagrapi password login); optional multi-user mode.
- **Gaps:** one set of metadata is shared by both platforms; the thumbnail is never sent to YouTube; captions are timed by spreading each line's duration across its words, not by real word timestamps; there are no automated tests.
- **Built but not working:** the `/api/pro` presets are never applied to processing; the batch and upload queues are stored but nothing processes them; analytics only accept hand-entered numbers; the editor endpoints run ffmpeg on file paths taken from the request. The UI calls none of the `/api/pro` routes, and the presets, batch and upload-queue tables are empty.

## Decisions (confirmed 7 Oct 2026)

The user approved the recommended option for each decision.

| ID | Decision | Chosen | Rejected alternative | Shapes |
|---|---|---|---|---|
| D1 | How Autopilot publishes | Official APIs only: YouTube Data API and Meta's Instagram API. The unofficial methods stay available for manual uploads. | Autopilot may also use the Studio browser automation and instagrapi | 8.1, 9.1, 11.1, 11.6 |
| D2 | Where sources come from | The user submits them: a YouTube URL, a file, or a list of URLs | Also watch the user's YouTube channel and clip new long videos automatically | 1.2, Out of scope |
| D3 | Editor scope | Focused clip editor (Requirement 7) | Full multi-track timeline with b-roll, transitions and joined clips | 7, Out of scope |

Assumptions (flag any that are wrong during review):

- One YouTube channel and one Instagram account per user.
- Review mode is the default; Full auto is switched on per profile.
- The spoken language is detected; captions and metadata follow it.
- Both run modes stay supported: local single-user, and multi-user with sign-in.

## Glossary

- **Source** — a long video submitted for clipping (YouTube URL or uploaded file).
- **Clip** — a span of a source chosen for a short. A clip can have several rendered **versions**.
- **Post** — one clip version going to one account on one platform, with that platform's metadata and a **slot** (planned go-live time).
- **Posting plan** — all posts not yet published.
- **Profile** — saved Autopilot settings applied to a source.
- **Hold** — a safeguard that keeps a post waiting for the user's approval in every mode.
- **Cleared** — a post that may publish: approved by the user, or in Full auto mode with no hold.
- **Official API** — YouTube Data API v3 with Google sign-in, or Meta's Instagram API with Meta sign-in.
- **Unofficial method** — the existing YouTube Studio browser automation (`youtube_pw.py`) or instagrapi password login (`instagram_upload.py`).
- **AI engine** — the configured language model: Kimchi today, with local Ollama and a keyword heuristic as fallbacks.

## Requirements

### Requirement 1: Run Autopilot on a source

**User story:** As a creator, I want to hand the app a long video and get finished, scheduled shorts back, so that one recording becomes a week of posts without me editing or writing anything.

**Acceptance criteria**

- **1.1** THE SYSTEM SHALL add an Autopilot switch and a profile picker to the existing submit form, remembering the user's last choice.
- **1.2** WHEN the user submits a YouTube URL, a video file, or a list of up to 20 YouTube URLs with Autopilot on, THE SYSTEM SHALL process each source in submission order — transcribe, choose clips, render, write metadata, plan posts — without further input.
- **1.3** WHILE a source is processing, THE SYSTEM SHALL show its current stage (downloading, transcribing, choosing clips, rendering clip N of M, writing metadata, planning posts) and an overall percentage.
- **1.4** WHEN the user closes the browser during processing, THE SYSTEM SHALL keep processing and show the results when the user returns.
- **1.5** IF the server restarts while a source is processing, THEN THE SYSTEM SHALL restart that source once after startup, and IF that attempt also fails, THEN mark the source failed and show the error.
- **1.6** IF a source is longer than 3 hours, THEN THE SYSTEM SHALL refuse it and state the limit.
- **1.7** IF no clip from a source reaches the profile's minimum score, THEN THE SYSTEM SHALL plan no posts for it and tell the user, showing the best score found.
- **1.8** WHEN the user submits a source with Autopilot off, THE SYSTEM SHALL produce clips and metadata in the library as it does today and SHALL plan no posts.
- **1.9** WHEN the user deletes a source that has unpublished posts, THE SYSTEM SHALL say how many posts will be cancelled and, on confirmation, cancel them, delete any not-yet-public YouTube uploads Autopilot made for them, and keep the history of posts already published.
- **1.10** WHEN the user tries a failed source again, THE SYSTEM SHALL process it again with the profile settings it was submitted with, replacing the clips the failed try made and counting the AI calls of every try; IF the source already has posts or its uploaded file is gone, THEN THE SYSTEM SHALL refuse and say why.

### Requirement 2: AI clip selection

**User story:** As a creator, I want the AI to pick moments that work as standalone shorts, so that every clip hooks in its first seconds and makes sense without the full video.

**Acceptance criteria**

- **2.1** WHEN an AI engine is configured, THE SYSTEM SHALL use it to choose each clip's start and end from the timestamped transcript.
- **2.2** THE SYSTEM SHALL keep each clip within the profile's length range (default 20–60 s, never above 180 s), start it at the beginning of a sentence, end it at the end of a sentence, and never cut mid-word.
- **2.3** THE SYSTEM SHALL score each clip 0–100 for viral potential with a one-line reason, and show the score and reason wherever the clip appears.
- **2.4** IF two candidate clips overlap by more than half the length of the shorter one, THEN THE SYSTEM SHALL keep only the higher-scoring clip.
- **2.5** IF the AI engine is unavailable or returns unusable output, THEN THE SYSTEM SHALL fall back to the local model and then the keyword heuristic, and label each clip with the method that chose it.
- **2.6** THE SYSTEM SHALL detect each source's spoken language and transcribe it in that language.
- **2.7** WHERE the profile sets a language, THE SYSTEM SHALL transcribe in that language instead of the detected one.

### Requirement 3: AI editing of each clip

**User story:** As a creator, I want each clip edited like a native short — speaker in frame, word-timed captions, a hook, no dead air, a good cover — so that it can go out as it is.

**Acceptance criteria**

- **3.1** THE SYSTEM SHALL render each clip as a 1080×1920 H.264/AAC MP4.
- **3.2** WHERE the profile's layout is Fill (the default), THE SYSTEM SHALL crop each frame to 9:16 so the detected face stays inside the crop in at least 95% of sampled frames, and SHALL hold the crop still while that face stays inside it.
- **3.3** IF no face is detected in a clip, THEN THE SYSTEM SHALL render that clip in the existing blurred-background Fit layout.
- **3.4** THE SYSTEM SHALL burn captions in the profile's style (words, phrases or karaoke), showing each word within 0.15 s of its timestamp in the transcript.
- **3.5** THE SYSTEM SHALL draw caption, hook and thumbnail text in a font that covers the transcript's script (for example Devanagari for Hindi), so that no character renders as a missing-glyph box.
- **3.6** WHERE trimming is on (the default), THE SYSTEM SHALL cut pauses longer than 0.5 s and filler-only phrases and keep captions in sync, as it does today.
- **3.7** WHERE the hook is on (the default), THE SYSTEM SHALL burn a 2–6 word hook over the first 2.5 s that differs from the title and from the first spoken line, as it does today.
- **3.8** THE SYSTEM SHALL pick a cover frame for each clip that is in focus and, when the clip contains a face, shows one.
- **3.9** THE SYSTEM SHALL add background music only from tracks the user has marked as cleared for publishing, ducked under speech as today; by default no track is added.

### Requirement 4: AI metadata for each platform

**User story:** As a creator, I want the AI to write the title, caption, hashtags and settings separately for YouTube and Instagram, so that each post fits its platform without me typing anything.

**Acceptance criteria**

- **4.1** WHEN a post is planned for YouTube, THE SYSTEM SHALL generate its title, description, tags, category and language.
- **4.2** WHEN a post is planned for Instagram, THE SYSTEM SHALL generate its caption, including hashtags.
- **4.3** THE SYSTEM SHALL keep generated text within each platform's documented limits: YouTube title at most 100 characters, description at most 5,000 bytes, tags at most 500 characters in total as YouTube counts them, and no `<` or `>` in any of them; Instagram caption at most 2,200 characters with at most 30 hashtags and 20 @-mentions.
- **4.4** THE SYSTEM SHALL write metadata in the clip's spoken language unless the profile sets a metadata language.
- **4.5** THE SYSTEM SHALL apply the profile's brand settings to all generated text: tone of voice, words to avoid, hashtags to always include, and the monetization kit's call-to-action and links.
- **4.6** WHEN the AI writes a title or caption, THE SYSTEM SHALL check it against the clip's transcript, and IF it claims something the transcript does not support, THEN regenerate it once and, IF the second version also fails the check, place a hold on the post.
- **4.7** THE SYSTEM SHALL set YouTube's "made for kids" flag from the channel audience the user confirms during setup, and SHALL NOT let the AI decide it.
- **4.8** THE SYSTEM SHALL leave each platform's AI-content disclosure (YouTube's altered or synthetic content flag, Instagram's AI label) off by default for clips cut from real footage, and SHALL let the user turn it on per profile or per post.
- **4.9** IF a post's metadata came from the keyword heuristic because no AI engine answered, THEN THE SYSTEM SHALL place a hold on the post.

### Requirement 5: Posting plan

**User story:** As a creator, I want the AI to decide which clip goes where and when, spread over good times, so that my accounts post steadily without bursts or gaps.

**Acceptance criteria**

- **5.1** WHEN a source finishes processing, THE SYSTEM SHALL plan one post per platform enabled in the profile for every clip that reaches the profile's minimum score (default 60).
- **5.2** THE SYSTEM SHALL give each post a slot inside the profile's posting windows (default: today's per-platform best-time windows) in the user's time zone, giving the earliest free slots to the highest-scoring clips.
- **5.3** THE SYSTEM SHALL keep at least the profile's minimum gap between posts on the same account (default 4 hours) and SHALL plan no more than the profile's daily maximum per account (default 2).
- **5.4** WHERE an account has at least 10 published posts with view counts, THE SYSTEM SHALL prefer the weekday-and-hour slots with the highest average views on that account, and show that the choice used the account's own data.
- **5.5** WHEN the user moves, removes or adds a post, THE SYSTEM SHALL keep that change and re-plan only posts the user has not changed.
- **5.6** THE SYSTEM SHALL store times in UTC and show them in the user's time zone with the zone's name.

### Requirement 6: Review queue and automation modes

**User story:** As a creator, I want to choose between approving each post and letting Autopilot publish on its own, so that I can start cautious and hand over control once I trust it.

**Acceptance criteria**

- **6.1** THE SYSTEM SHALL offer two modes per profile: Review (the default) and Full auto.
- **6.2** WHILE a profile is in Review mode, THE SYSTEM SHALL publish none of its posts until the user approves them.
- **6.3** IF a Review-mode post reaches its slot unapproved, THEN THE SYSTEM SHALL NOT publish it, SHALL release the slot, and SHALL give the post the next free slot when the user approves it.
- **6.4** WHILE a profile is in Full auto mode, THE SYSTEM SHALL treat its posts as cleared unless they are under a hold.
- **6.5** THE SYSTEM SHALL show a review queue that lists each upcoming post with its vertical preview, platform, account, slot, score and reason, any hold and why, and every generated field, and that lets the user approve, edit, reschedule or reject each post.
- **6.6** WHEN the user selects several posts (for example everything from one source or one day) and approves or rejects them, THE SYSTEM SHALL apply the action to every selected post.
- **6.7** WHEN the user edits a generated field, THE SYSTEM SHALL keep the AI's version and offer "Reset to AI version".
- **6.8** THE SYSTEM SHALL offer one "Pause all publishing" switch whose state is visible on every page that lists posts.
- **6.9** WHILE the pause is on, THE SYSTEM SHALL start no publish attempt and SHALL stop already-uploaded YouTube videos from going live at their scheduled time.
- **6.10** WHEN the user turns the pause off, THE SYSTEM SHALL give posts whose slots passed during the pause the next free slots and resume publishing.

### Requirement 7: Clip editor

**User story:** As a creator, I want to fix any clip — trim it, cut words, correct captions, reframe it, change the hook, cover or music — so that I can correct what the AI got wrong before it posts.

**Acceptance criteria**

- **7.1** WHEN the user opens a clip in the editor, THE SYSTEM SHALL show the vertical preview, the clip's transcript with word timings, and a timeline of the source from at least 30 s before to 30 s after the clip, with in and out handles.
- **7.2** WHEN the user moves a handle, THE SYSTEM SHALL update the preview within 200 ms and allow any range inside the source, warning when the length exceeds a target platform's limit.
- **7.3** WHEN the user selects words in the transcript and deletes them, THE SYSTEM SHALL cut those words' time spans from the clip.
- **7.4** WHEN the user corrects a caption word, THE SYSTEM SHALL change its text in the burned captions and keep its timing.
- **7.5** THE SYSTEM SHALL let the user change the caption style or turn captions off; edit or remove the hook; switch between Fill and Fit and drag the Fill crop sideways; choose, change or remove music and set its volume; and pick the cover frame by scrubbing.
- **7.6** THE SYSTEM SHALL preview trims, word cuts, caption text, the hook and the crop in the browser before rendering, marked as a preview.
- **7.7** WHEN the user renders, THE SYSTEM SHALL render a new version in the background, keep the previous version until the new one succeeds, and then use the new version in all of the clip's unpublished posts.
- **7.8** IF rendering fails, THEN THE SYSTEM SHALL keep the previous version in use and show the error.
- **7.9** IF a post of the clip is already published, THEN THE SYSTEM SHALL tell the user before rendering that published posts will not change.
- **7.10** THE SYSTEM SHALL let the user undo and redo every edit made since the editor was opened.

### Requirement 8: Publish to YouTube

**User story:** As a creator, I want posts to go out as YouTube Shorts through YouTube's official API, on time even when my laptop is asleep at the slot, so that uploads keep working unattended without putting my channel at risk.

**Acceptance criteria**

- **8.1** THE SYSTEM SHALL publish Autopilot's YouTube posts only through the YouTube Data API, to the channel connected with Google sign-in.
- **8.2** WHEN a cleared YouTube post that targets Public has a slot less than 24 hours away, THE SYSTEM SHALL upload it as private with YouTube's scheduled publish time set to the slot, so it goes live on time even if the app is not running then.
- **8.3** WHEN a cleared YouTube post that targets Private reaches its slot, THE SYSTEM SHALL upload it as private.
- **8.4** WHEN the metadata of an uploaded, not-yet-live post is edited, THE SYSTEM SHALL update the video on YouTube.
- **8.5** WHEN an uploaded, not-yet-live post is rejected or deleted, THE SYSTEM SHALL delete that upload from YouTube.
- **8.6** WHEN an uploaded, not-yet-live post gets a new clip version, THE SYSTEM SHALL ask the user to confirm and then replace that upload with the new version.
- **8.7** WHEN a YouTube post's slot has passed, THE SYSTEM SHALL read the video's visibility from YouTube, and IF it differs from the post's target, THEN mark the post "Kept private by YouTube" and explain that videos uploaded by a Google Cloud project that has not passed YouTube's API audit stay private, linking to the audit form.
- **8.8** WHEN a YouTube upload succeeds, THE SYSTEM SHALL store the video ID and link with the post.
- **8.9** WHERE YouTube accepts a custom thumbnail for the uploaded video, THE SYSTEM SHALL set the clip's cover frame; otherwise THE SYSTEM SHALL show that YouTube chooses the frame.

### Requirement 9: Publish to Instagram

**User story:** As a creator, I want posts published as Instagram Reels through Meta's official API, so that posting works unattended without my account being flagged.

**Acceptance criteria**

- **9.1** THE SYSTEM SHALL publish Autopilot's Instagram posts only through Meta's Instagram API, to the professional account connected with Meta sign-in.
- **9.2** WHEN a cleared Instagram post reaches its slot, THE SYSTEM SHALL publish its vertical version as a Reel with the generated caption, the clip's cover frame and the AI-disclosure setting; whether the Reel also appears in the main feed follows Instagram's default, because the upload route for local files does not accept that option.
- **9.3** WHILE Instagram is processing an uploaded video, THE SYSTEM SHALL check its status no more than once a minute, and IF it is not ready after 5 minutes or Instagram reports an error, THEN treat the attempt as a temporary failure (10.2).
- **9.4** WHEN an Instagram post is published, THE SYSTEM SHALL store its media ID and link with the post.
- **9.5** IF Instagram reports that the account's publishing limit is reached, THEN THE SYSTEM SHALL move the post to the first free slot after the limit resets and tell the user.

### Requirement 10: Publishing reliability

**User story:** As a creator running Autopilot unattended, I want publishing to be predictable — never duplicated, never bursty, with clear failures — so that a glitch never spams or embarrasses my accounts.

**Acceptance criteria**

- **10.1** THE SYSTEM SHALL publish each post at most once: IF an attempt was interrupted (crash, restart, lost connection), THEN before retrying THE SYSTEM SHALL ask the platform whether the post already exists and treat a found post as published.
- **10.2** IF an attempt fails with a temporary error (lost connection, platform server error, rate limiting), THEN THE SYSTEM SHALL retry after about 2, 10 and 30 minutes, and IF the last retry fails, THEN mark the post failed and notify the user.
- **10.3** IF an attempt fails with a permanent error (rejected or revoked access, policy rejection, invalid metadata or file), THEN THE SYSTEM SHALL NOT retry, and SHALL mark the post failed with the platform's reason and notify the user.
- **10.4** THE SYSTEM SHALL count uploads against each platform's limit and SHALL NOT start one that would exceed it: on YouTube, 100 uploads per day per Google Cloud project, shared by every user of a deployment; on Instagram, the limit Meta reports for the account.
- **10.5** IF posts' slots passed while the app was not running, THEN at startup THE SYSTEM SHALL publish only the most recent missed post per account, and only when its slot passed less than 2 hours ago, and SHALL give every other missed post the next free slot.
- **10.6** THE SYSTEM SHALL refresh access tokens before they expire, and WHERE a platform does not allow renewing access without the user, THE SYSTEM SHALL ask the user to reconnect at least 7 days before access lapses.
- **10.7** IF a token refresh fails or an account's access is revoked, THEN THE SYSTEM SHALL stop publishing to that account, mark its posts "needs reconnect" and notify the user with the reason; after reconnection, its posts whose slots passed SHALL get the next free slots.
- **10.8** WHEN a post is about to be published, THE SYSTEM SHALL check its file against the platform's format limits (duration, file size, resolution, aspect ratio, codecs), and IF a limit is broken, THEN block the post and name the limit.
- **10.9** THE SYSTEM SHALL run uploads in the background, one at a time per account.
- **10.10** WHEN the user tries a failed post again, THE SYSTEM SHALL give it fresh retries without ever publishing it twice: a post with nothing on the platform yet SHALL return to the plan with the clip's current version, at its own time if that is still ahead or else at the next free slot, and a post the platform already holds part of SHALL carry on from the step that failed. Manually scheduled uploads SHALL keep today's rule and not be tried again.

### Requirement 11: Connections and readiness

**User story:** As a creator setting up Autopilot, I want a checklist that connects my accounts the official way and tells me exactly what's missing, so that I can switch Autopilot on with confidence.

**Acceptance criteria**

- **11.1** THE SYSTEM SHALL show a readiness checklist with required items — YouTube connected with Google sign-in, channel audience (made for kids or not) confirmed, Instagram professional account connected with Meta sign-in — and advisory items — AI engine connected, Google OAuth app set to "In production" (apps left in "Testing" get refresh tokens that expire after 7 days), and YouTube API audit passed (as declared by the user) — plus the time zone in use with a way to change it.
- **11.2** IF a required item for a platform is missing, THEN THE SYSTEM SHALL keep Autopilot publishing to that platform off and show a fix-it step for the item.
- **11.3** IF an advisory item is missing, THEN THE SYSTEM SHALL allow Autopilot and show the consequence (for example, "YouTube uploads will stay private").
- **11.4** WHEN the user connects Instagram, THE SYSTEM SHALL use Meta's official sign-in, and IF the account does not meet Meta's requirements for API publishing (for example, it is a personal account), THEN say which requirement is missing and how to fix it.
- **11.5** THE SYSTEM SHALL show each connection's account name, connection date, token expiry where known, and last successful publish.
- **11.6** WHERE an account is connected only through an unofficial method, THE SYSTEM SHALL keep that connection for manual uploads, SHALL NOT use it for Autopilot, and SHALL say why: these methods break the platforms' terms and risk blocks or bans.
- **11.7** WHEN the user disconnects an account, THE SYSTEM SHALL delete its stored tokens and mark its unpublished posts "needs reconnect".
- **11.8** WHEN asking for access, THE SYSTEM SHALL request only the permissions the enabled features need and explain each one on the connect screen.

### Requirement 12: Activity, notifications, history and performance

**User story:** As a creator, I want to see everything Autopilot decided and did, and how each post performed, so that I can trust it, catch problems early and improve.

**Acceptance criteria**

- **12.1** THE SYSTEM SHALL record an activity entry for every Autopilot decision and action — source processed, clips chosen with scores, posts planned, approved, edited, rescheduled, published, failed, retried, paused — with its time, its actor (AI, user or system) and its outcome, and keep entries for at least 90 days.
- **12.2** THE SYSTEM SHALL show the number of failed posts and posts waiting for approval on every page.
- **12.3** WHERE the user has allowed browser notifications, THE SYSTEM SHALL show one when a post fails or needs approval.
- **12.4** THE SYSTEM SHALL show a post history listing each published post's platform, account, publish time, link and status.
- **12.5** THE SYSTEM SHALL fetch views, likes and comments for each published post from the official APIs at least once a day for 30 days after it is published, and show them in the post history.

### Requirement 13: Rights and safety safeguards

**User story:** As a creator, I want Autopilot to stop at anything that could get my accounts struck, so that automation never posts something I would regret.

**Acceptance criteria**

- **13.1** WHEN the user turns on publishing for a source, THE SYSTEM SHALL ask them to confirm they own the content or have the right to republish it, showing the source channel's name for URL sources, and SHALL plan no posts for the source until they confirm.
- **13.2** IF a URL source was published by the user's connected YouTube channel, THEN THE SYSTEM SHALL skip that confirmation.
- **13.3** WHEN the AI flags a clip as possibly sensitive (strong profanity, sexual content, violence, hateful language, or health or financial claims), THE SYSTEM SHALL place a hold on its posts and show the reason.
- **13.4** IF a clip overlaps by more than half a clip from the same source that is already published to the same account, THEN THE SYSTEM SHALL NOT plan it for that account.

### Requirement 14: Autopilot profiles

**User story:** As a creator, I want to save my Autopilot settings as profiles, so that each new video is handled the same way without re-entering choices.

**Acceptance criteria**

- **14.1** THE SYSTEM SHALL let the user create, edit, duplicate and delete profiles that hold: clips per source, clip length range, minimum score, transcript and metadata language, layout, caption style, hook, trimming, music and volume, platforms, YouTube target visibility (Public or Private), posting windows, minimum gap, daily maximum, automation mode, brand settings and disclosure defaults.
- **14.2** THE SYSTEM SHALL provide a default profile: 6 clips per source, 20–60 s, minimum score 60, detected language, Fill layout, word captions, hook on, trimming on, no music, both platforms, Public, today's best-time windows, a 4-hour gap, 2 posts per account per day, Review mode, and AI disclosure off.
- **14.3** WHEN the user submits a source, THE SYSTEM SHALL apply the chosen profile (the default when none is chosen) and record the settings used with the source.
- **14.4** WHEN the user edits a profile, THE SYSTEM SHALL apply the change to later submissions only, and SHALL offer "Re-plan pending posts" to apply its posting settings to unpublished posts the user has not changed.

### Requirement 15: Replace the unfinished `/api/pro` endpoints

**User story:** As the app's owner, I want the half-built pro endpoints replaced by these features, so that each capability has one working implementation and no endpoint trusts file paths or user IDs sent by the browser.

**Acceptance criteria**

- **15.1** THE SYSTEM SHALL remove the `/api/pro/editor/*` endpoints, which run ffmpeg on file-system paths taken from the request; the clip editor SHALL refer to clips only by IDs the signed-in user owns.
- **15.2** THE SYSTEM SHALL remove `/api/pro/batch`, `/api/pro/schedule/*`, `/api/pro/presets` and `/api/pro/analytics/*`, replacing them with the multi-source queue (1.2), the posting plan (5), profiles (14) and post history (12).
- **15.3** THE SYSTEM SHALL take the user's identity only from the session, never from a request parameter (today `/api/pro` reads `user_id` from the query string).
- **15.4** WHEN the upgraded app first starts, THE SYSTEM SHALL carry existing scheduled uploads into the posting plan and post history without changing their content, times or status, and manually scheduled uploads SHALL keep the upload method they use today.

## Non-functional requirements

### NFR 1: Performance (reference machine: Apple M4 MacBook Air)

- **N1.1** A 60-minute source SHALL finish processing within 30 minutes, not counting download time.
- **N1.2** API requests from the UI SHALL answer within 500 ms at the 95th percentile while processing and uploads run.
- **N1.3** The review queue SHALL display 200 posts within 1 second.
- **N1.4** Rendering a new version of a 60-second clip SHALL take at most 3 minutes.

### NFR 2: Reliability

- **N2.1** All Autopilot state — sources, clips, versions, posts, publish attempts, profiles, connections and activity — SHALL be stored in the database and survive restarts.
- **N2.2** A test that stops the process in the middle of an upload and restarts it SHALL find the post published exactly once (10.1).

### NFR 3: Security and privacy

- **N3.1** Platform tokens and the AI key SHALL stay on the server: never sent to the browser, never written to logs, and stored only where the OS account running the app can read them.
- **N3.2** Every new endpoint SHALL take the user from the session and accept clips and posts only as IDs.
- **N3.3** Every OAuth callback SHALL reject a missing or mismatched `state` value, as the existing YouTube callback does.
- **N3.4** THE SYSTEM SHALL send transcripts and metadata only to the configured AI engine, and media only to the connected platforms.

### NFR 4: Multi-user isolation

- **N4.1** WHILE multi-user mode is on, every source, clip, post, profile, connection and activity entry SHALL be visible and changeable only by its owner.
- **N4.2** Isolation tests SHALL check both directions: the owner can act on their own data, and another signed-in user cannot.

### NFR 5: Accessibility

- **N5.1** The editor, review queue and readiness checklist SHALL be fully usable by keyboard, with editor handles moving 0.1 s per arrow-key press and 1 s with Shift.
- **N5.2** These screens SHALL meet WCAG 2.1 AA: visible focus, labelled controls, text contrast of at least 4.5:1, and status changes announced to screen readers. White text on the theme's terracotta `#C4612F` measures about 4.1:1, so text-bearing buttons on these screens need the darker `#A94E22` (about 5.5:1) or large text.

### NFR 6: Compatibility

- **N6.1** Existing flows SHALL keep working: processing by URL or file, the library, manual upload and schedule buttons, the monetization kit, music upload, AI engine settings, and both sign-in modes.
- **N6.2** The existing `clipmaker.db` SHALL be upgraded in place with no loss of jobs, schedules, users or settings.

### NFR 7: Testability

- **N7.1** Automated tests SHALL run offline against fake YouTube, Instagram and AI engine clients, and no test SHALL make a real upload or AI call. The project has no tests today; this feature adds the suite.

### NFR 8: AI usage

- **N8.1** Processing a source with N clips for the first time SHALL use at most 3N + 3 AI engine calls, and THE SYSTEM SHALL show how many calls each source used.

## Out of scope

- Publishing to TikTok, X, Facebook or LinkedIn.
- Watching a channel, playlist or folder for new videos (D2).
- More than one account per platform per user.
- Multi-track timeline editing: b-roll, transitions, joining clips (D3).
- Generated media: AI voice-over, AI b-roll, avatars, dubbing.
- Posting or pinning comments, and replying to comments or messages.
- Instagram trial Reels, Stories, carousels, collaborators, and location or people tags.
- Title A/B tests and paid promotion.
- Making the unofficial upload methods safe for unattended use (D1).

## Revision notes

- **9.2 (7 Oct 2026, design):** Meta's resumable-upload guide lists the Reel parameters it accepts and says "other parameters are not supported"; `share_to_feed` is not among them. Resumable upload is the only documented way to send a local file, so the share-to-feed setting was removed.
- **10.6 (7 Oct 2026, design):** Google refresh tokens renew silently, but Meta access obtained through Facebook Login cannot always be renewed without the user. The criterion now also covers asking for a reconnect before access lapses.
- **1.10 (18 Oct 2026, after task 13.2):** added at the user's request from the usability review's open decisions (design §15): a failed source can be tried again instead of being added again.
- **10.10 (18 Oct 2026, after task 13.2):** added the same way: a failed post can be tried again instead of only being dropped. 10.2 and 10.3 still decide when a post fails.

## Platform constraints (checked against official docs, 7 Oct 2026)

- **YouTube Data API** — [videos.insert](https://developers.google.com/youtube/v3/docs/videos/insert) (page updated 14 Sep 2026): uploads have their own quota of 100 per day per project. Videos uploaded by unverified API projects created after 28 July 2020 are restricted to private until the project passes an audit. Settable fields include `status.publishAt` (scheduled publishing), `status.selfDeclaredMadeForKids`, `status.containsSyntheticMedia` and `snippet.defaultLanguage`.
- **Google OAuth** — [refresh token expiration](https://developers.google.com/identity/protocols/oauth2#expiration) (page updated 26 May 2026): an external OAuth app in "Testing" status gets refresh tokens that expire in 7 days.
- **Instagram API** — [content publishing](https://developers.facebook.com/docs/instagram-platform/content-publishing/) (page updated 30 Jun 2026): professional accounts only. The rate-limit section allows 100 API-published posts per 24-hour moving window (the carousel section of the same page still says 50); `content_publishing_limit` reports the live value, which is why 10.4 trusts Meta's figure. Uploading a local file (resumable upload to `rupload.facebook.com`) is documented for apps using Facebook Login for Business; the Instagram Login route fetches the video from a public URL. Containers not published within 24 hours expire, and Meta recommends checking a container's status once a minute for no more than 5 minutes. `is_ai_generated` sets the AI disclosure.
