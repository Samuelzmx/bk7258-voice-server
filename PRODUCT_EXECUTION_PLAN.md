# BK7258 AI Toy Product Execution Plan

## Goal

Turn the current working BK7258 voice server into a product that a parent can set up and use without engineering help.

## Product Direction

The product should move in this order:

1. keep the current working chip voice workflow stable
2. make setup much simpler
3. make the parent control panel feel like a real app
4. add reusable educational and story content
5. package the whole system so a family does not need Terminal knowledge

## Stable Baseline

The current proven baseline is:

- BK7258 chip connects to the Python WebSocket server over Wi-Fi
- server handles STT -> LLM -> TTS
- chip can hear and speak
- parent can control the toy from a phone-first LAN panel
- LLM provider and model can be changed at runtime
- low-latency speech path is already in place

This baseline should stay unchanged on the stable branch.

## Experimental Product Layer

The current experimental product layer adds:

- persistent family setup
- toy name
- parent name
- child name
- child age band
- child interests
- parent goals
- safety mode
- selectable learning packs
- selectable story library
- structured content JSON files
- content reload without Python edits
- a phone onboarding page
- a fully local onboarding QR path
- optional local parent access-code protection
- persistent recent-turn and recent-session history for parents
- richer parent dashboard summaries and activity filters
- age-aware content recommendations and parent-side content filtering
- latency-focused defaults for shorter replies, shorter LLM context, and faster turn commit

These settings feed directly into the runtime system prompt so the toy can behave more like a real personalized product.

## Phase 1: Family Beta Hardening

### Objective

Make one toy reliable for one family on one home Wi-Fi network.

### Build

- keep the current voice workflow stable
- keep low-latency local TTS enabled by default
- persist family setup to disk
- expose product state in the control panel
- keep direct speak and status tools for debugging

### Verify

- chip stays connected for a 30-minute session
- parent changes a mode in the panel and the next reply reflects it
- product-state changes survive a server restart
- manual speak still reaches the chip

## Phase 2: Setup Simplification

### Objective

Reduce setup to a short checklist that a non-technical tester can follow.

### Build

- one `setup_server.command` or equivalent guided installer
- one `start_server.command` launcher
- BKFIL flashing steps with exact download link and click path
- one place for Wi-Fi and server IP instructions
- QR code page that opens the local parent panel on a phone

### Verify

- a fresh Mac can be prepared in under 15 minutes
- a tester can flash the chip and start the server without editing code
- the phone opens the parent panel by scanning or tapping one link

## Phase 3: Parent App V1

### Objective

Make the control panel feel like a product instead of an internal tool.

### Build

- password or pairing-code protection
- parent dashboard for profiles and modes
- one-tap mode switching
- usage and last-session visibility
- settings for provider, model, and API key

### Verify

- another device on the same Wi-Fi can open the panel and configure the toy
- unauthorized users cannot change the toy without the pairing code
- the parent can switch from companion mode to storyteller mode in one action

## Phase 4: Content System

### Objective

Make the toy useful for repeat daily use.

### Build

- structured story packs
- structured lesson packs
- age-tagged content
- simple retrieval by profile and mode
- optional family-safe memory and progress tracking

### Verify

- a parent can choose a library pack and hear different behavior immediately
- stories stay age-appropriate
- language practice feels consistent across multiple turns

## Phase 5: Real Consumer Delivery

### Objective

Ship a version that does not depend on manual local engineering steps.

### Build

- packaged desktop app or mini-server appliance
- real mobile app or polished PWA
- account and subscription path if desired
- hosted backend option
- preflashed or factory-flashed device flow

### Verify

- setup feels like normal consumer onboarding
- parent does not need Terminal
- parent does not need to know server ports or environment variables

## Immediate Next Engineering Tasks

1. Keep `main` as the stable working branch.
2. Move product work to a separate branch such as `product-beta-foundation`.
3. Keep testing the family setup UI against the live chip workflow.
4. Add richer content metadata and retrieval beyond simple tag matching.
5. Start separating product UI concerns from the single-file server implementation.
6. Move toward a packaged parent app or PWA shell around the current LAN panel.

## Definition Of Success

The next milestone is reached when:

- the chip still talks reliably
- the parent can configure child profile and modes from the panel
- those settings persist after restart
- the system is documented clearly enough for another engineer to continue
