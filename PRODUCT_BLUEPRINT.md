## BK7258 AI Toy Product Blueprint

### Product Goal

Turn the current BK7258 voice server into a real consumer product:

1. a parent gets the toy
2. the parent installs one app or opens one setup page
3. the parent connects the toy to Wi-Fi
4. the parent chooses the toy personality, learning packs, and story library
5. the child talks to the toy naturally

### Core Product Experience

#### Parent experience

- open the phone app or phone web app
- pair the toy
- connect the toy to home Wi-Fi
- choose the child profile
- choose goals like:
  - English learning
  - bedtime stories
  - social skills
  - curiosity and questions
- choose the LLM provider and model
- provide their own API key or buy a managed subscription
- review toy usage, favorite stories, and learning progress

#### Child experience

- press once or wake hands-free
- hear a friendly character voice
- ask questions
- listen to stories
- play simple learning games
- practice words and phrases

### Product Architecture

#### Today

- BK7258 chip connects by Wi-Fi WebSocket to the Python server
- server does STT -> LLM -> TTS
- phone opens a LAN control panel

#### Next product architecture

1. Toy firmware
- stable Wi-Fi onboarding
- robust reconnect logic
- wake interaction
- audio streaming

2. Local parent control
- phone-first control panel
- child profile setup
- story and learning pack selection
- provider/model/API-key setup

3. Managed content layer
- curated story library
- curated learning packs
- content safety filters
- usage analytics

4. Real app
- iOS / Android wrapper or React Native app
- login
- pairing
- subscription or API-key setup
- remote access if desired

### Product Milestones

#### Milestone 1: Family Beta Foundation

Goal:
- one family can use one toy on one home network

Must have:
- working chip voice pipeline
- low-latency TTS
- phone-first parent panel
- persistent family setup
- starter learning packs
- starter story library

Status:
- mostly done in the current repo

#### Milestone 2: Setup Simplicity

Goal:
- parent setup takes under 10 minutes without coding

Must have:
- simple flashing guide or preflashed units
- Wi-Fi onboarding flow
- QR code pairing
- password-protected parent panel

Next engineering tasks:
- add pairing code / password auth
- add QR code page for phone onboarding
- make firmware setup less manual

#### Milestone 3: Managed Content and Customization

Goal:
- parent can shape the toy by topic and learning purpose

Must have:
- profile per child
- curated story sets
- curated learning tracks
- persistent toy memory scoped to the family
- local-library RAG or cloud RAG

Next engineering tasks:
- move local story library to structured content files
- add content tagging by age, skill, and topic
- add vector search or simple retrieval

#### Milestone 4: Consumer App

Goal:
- parent installs one app and manages everything there

Must have:
- real mobile app
- account login
- toy pairing
- child dashboards
- remote control and content sync

Suggested stack:
- React Native app
- same Python server API initially
- later move to dedicated backend service

### RAG Plan

#### Phase 1: Local library prompting

Current direction:
- selected story cards and learning packs are injected into the prompt

Why:
- fast to ship
- easy to test
- enough for an early family beta

#### Phase 2: Structured retrieval

Move stories and learning content into:
- JSON or Markdown content files
- tagged by age, topic, difficulty, language target

Retrieve:
- by child profile
- by parent goal
- by active mode

#### Phase 3: Full RAG

Add:
- embedding index
- semantic search
- progress-aware retrieval
- family-safe content filtering

### Recommended Build Order

1. Stabilize current beta workflow
- keep chip connection reliable
- keep latency low
- verify parent panel controls

2. Make onboarding easier
- password/pairing code
- QR code for phone access
- simpler firmware/setup guide

3. Add persistent family product data
- child profile
- learning packs
- story library
- saved voice and character settings

4. Add managed content
- story packs
- language lessons
- simple games

5. Build real mobile app
- app shell first
- full onboarding second

### Success Metrics

#### Setup

- first-time setup under 10 minutes
- parent never edits code
- parent never needs Terminal after initial install

#### Reliability

- toy stays connected for 30+ minutes
- reconnects cleanly after power cycle
- parent panel reflects live state accurately

#### Latency

- toy starts audible response within 1 second when possible
- full turn feels conversational for short prompts

#### Product usefulness

- parent can switch modes in under 10 seconds
- child can get a story or lesson in one tap
- content feels age-appropriate and personalized

### What To Build Next In Code

1. Add password or pairing-code protection to the control panel
2. Add QR code onboarding page for the phone
3. Move family setup into a dedicated parent section with saved persistence
4. Store story library and learning packs in structured content files
5. Add session memory scoped to the child profile
6. Build simple progress tracking

### What You May Need To Do

If you want this to become a real product faster, the most helpful non-code steps are:

1. Decide whether the first launch is:
- self-hosted by the parent on their Mac
- or bundled with a preconfigured mini server

2. Decide the business model:
- user brings their own API key
- or managed subscription with your hosted keys

3. Decide the first child use case:
- language learning first
- or story companion first

The best first product target is:
- story companion + English learning
- one child profile
- one phone-first parent control surface
