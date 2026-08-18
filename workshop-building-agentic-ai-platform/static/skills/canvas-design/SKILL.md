---
name: canvas-design
description: Create beautiful visual art in .png and .pdf documents using design philosophy.
version: 1.0.0
tags: [design, canvas, art]
---

# Canvas design

Guidance for an agent that produces a standalone visual artefact — a diagram, poster,
or one-page summary exported as `.png` or `.pdf` — rather than an interactive screen.

## When to use this skill

Use it when the deliverable is a fixed-size image or document that will be read
without interaction: an architecture diagram, a results summary, a slide. Do not use
it for interactive interfaces; use `frontend-design` for those.

## Approach

1. **Fix the canvas size and margin before drawing anything.** The margin is part of
   the composition, not leftover space. A generous, uniform margin does more for
   legibility than any other single choice.
2. **Decide the focal point.** One element should be unambiguously first. Establish it
   with size or position, not with a fourth colour.
3. **Align to a grid and keep alignment visible.** Elements that share a purpose
   should share an edge. Near-alignment reads as error; deliberate offset reads as
   intent.
4. **Limit the palette.** One accent, one neutral ramp, and white space. Reserve the
   accent for the element you want read first.
5. **Set type in no more than two sizes plus a caption size.** Distinguish levels by
   weight and space rather than by adding sizes.
6. **Label directly.** Put labels next to what they describe instead of in a legend
   whenever the geometry allows it — a legend forces the reader to hold a mapping in
   memory.

## Export requirements

- Vector output (`.pdf`, or `.png` rendered at 2× or higher) so text stays sharp when
  projected or zoomed.
- Text as text, not as rasterised pixels, wherever the format allows it.
- Legible when printed in greyscale: check that the composition survives losing
  colour, since meaning must not depend on hue alone.
- Contrast of at least 4.5:1 for body text and 3:1 for large text and for the
  boundaries of shapes that carry meaning.

## Output

Return the artefact plus a one-line statement of the focal point and the reading order
you designed for, so the choice can be reviewed rather than guessed at.
