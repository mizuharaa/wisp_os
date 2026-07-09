# design-intelligence

Product/UI design intelligence as a dependency-ordered progression: master the
foundations first, then branch into the structural (dashboard/UX) or emotional
(retention/delight) path depending on what the product needs. A level-up map,
not a flat list of tips.

- **Trigger:** /design-iq
- **Earn:** `python skills/engine.py use design-intelligence` after each surface it shaped
- **Reference:** study real-app pattern libraries before inventing new patterns

## The tree (foundational skills unlock advanced ones)

### Tier 1 — Foundational (unlocks everything)
- **Data-Driven Form** — UI structure is *derived from the data's nature*, not
  chosen arbitrarily. Chips for categorical data, right-align numbers, timelines
  for time-series, truncation/shading for low-priority text. → unlocks Visual
  Hierarchy, Progressive Disclosure.
- **Emotional Baseline** — functionality is commoditized (APIs / no-code / AI);
  *feeling is the differentiator*. → unlocks Micro-interactions, Delight, Polish.

### Tier 2 — Structural (need Tier 1)
- **Visual Hierarchy** (needs Data-Driven Form) — contrast + prioritization
  decide what's seen first vs de-emphasized. Color is *functional, not
  decorative*: red = urgency, avatars = fast person-recognition.
- **Progressive Disclosure** (needs Data-Driven Form) — show only the essential
  upfront; reveal the rest on demand. Spectrum: always-visible (primary) →
  hover-only (secondary, e.g. "remove user") → tucked in popovers (rare, e.g.
  "share"). Ref: Apple Reminders swipe-to-reveal.
- **Intentional Polish** (needs Emotional Baseline) — animation/transition/detail
  is a *feature*, not decoration. Builds trust, especially in intimidating
  domains (fintech, crypto).

### Tier 3 — Applied systems (need Tier 2)
- **Onboarding Sequencing** (needs Progressive Disclosure) — don't dump one
  modal; sequence learning. One tooltip on the #1 action → gradual checklist.
- **Invisible UI / Orchestration** (needs Progressive Disclosure + Hierarchy) —
  hidden components (tooltips, copy buttons, modals, comment indicators) separate
  beginner dashboards from mature ones. Orchestrate spacing/sizing/hidden states
  so complexity doesn't spawn new pages.
- **Emotional Feedback Loops** (needs Polish) — micro-interactions that respond
  emotionally to actions (success/failure beyond binary). Duolingo's character
  animations tracked DAUs 14.2M → 34M in two years.
- **Humanizing Complexity** (needs Polish + Feedback Loops) — friendly design +
  mascots make intimidating domains approachable. Phantom's ghost mascot → #2
  US utility app.
- **Habit Formation & Rewards** (needs Feedback Loops) — celebrate small wins to
  reinforce return usage. Revolut's 3D card flips / tactile charts tie premium
  feel to trust and revenue.

### Tier 4 — Meta (governs all)
- **Reference Library Usage** — don't reinvent patterns; study curated real-app
  libraries (e.g. Mavin) to shortcut Tiers 1–3.

## Quick-reference (one-line rule per skill)

1. Data-Driven Form — *let the data shape the layout*
2. Emotional Baseline — *feeling > features now*
3. Visual Hierarchy — *color/contrast = meaning, not decoration*
4. Progressive Disclosure — *show little, reveal more on demand*
5. Intentional Polish — *animation is a feature*
6. Onboarding Sequencing — *teach one thing at a time*
7. Invisible UI / Orchestration — *hidden ≠ absent; it's designed*
8. Emotional Feedback Loops — *react to the user, not just log the action*
9. Humanizing Complexity — *mascots/friendliness lower intimidation*
10. Habit Formation — *reward small wins to build retention*
11. Reference Library Usage — *study real patterns before inventing new ones*

## How to apply here

Maestro's own console follows web-design taste (royal-plum, white cards,
per-service brand colors). This skill is the *why* behind those moves: the pulse
cards are Data-Driven Form (each service its own brand), the hover-reveal navbar
and setup buttons are Progressive Disclosure, the iPhone-style timer roll is
Intentional Polish, and the weather chip is Humanizing Complexity.
