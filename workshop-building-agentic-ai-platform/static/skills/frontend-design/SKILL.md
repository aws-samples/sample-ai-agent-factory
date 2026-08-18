---
name: frontend-design
description: Create distinctive frontend interfaces with high design quality suitable for workshop demonstrations.
version: 1.0.0
tags: [design, frontend, ui]
---

# Frontend design

Guidance for an agent that builds or reviews a web frontend for a demonstration
application — the FAST agent UI in this workshop is a working example.

## When to use this skill

Use it when the task is to produce a user-facing screen and the request does not
already specify a design system. Do not use it for backend work, for data modelling,
or when an existing component library must be followed exactly; in that case follow
the library.

## Approach

1. **Establish the job of the screen first.** Name the single decision or action the
   screen exists to support. Everything that does not serve that decision is a
   candidate for removal.
2. **Choose a layout before choosing colours.** Decide the reading order, then the
   grid, then the spacing scale. Colour and type are the last decisions, not the
   first.
3. **Pick one accent and one neutral ramp.** A single accent colour plus a neutral
   ramp reads as deliberate. Three accents read as unfinished.
4. **Use a spacing scale, not arbitrary pixel values.** A geometric scale
   (4, 8, 12, 16, 24, 32, 48) keeps rhythm consistent without per-element decisions.
5. **Show state.** Every asynchronous action needs a visible loading, empty, and
   error state. A demonstration that only renders the happy path fails in front of
   an audience.

## Accessibility requirements

These are requirements, not preferences:

- Text contrast of at least 4.5:1 against its background (3:1 for text above 18pt).
- Every interactive element reachable and operable by keyboard, with a visible focus
  ring. Do not remove the focus outline without replacing it.
- Headings in hierarchical order with no skipped levels.
- Meaningful images carry alternative text; decorative images are marked as
  decorative so screen readers skip them.
- Never encode meaning in colour alone — pair it with a label, icon, or shape.

## Output

Return the component or page source together with a short note on the layout
decision and any accessibility trade-off that was made and why.
