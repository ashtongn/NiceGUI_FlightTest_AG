# Operator console: layout options

## Current problems (/ route, 1440x900 and 1920x1080)
- Single column, about 1740 px tall, so it scrolls at both sizes. The page needs about 2x the viewport height.
- Control card (tuning, RX, TX), spectrum, IQ playback, jammer, detections, decodes and log stack vertically. Spectrum is split by the fixed footer when scrolling.
- The radio is named only in the "focus" label near the top. After scrolling down to Jammer or IQ playback, nothing says which radio those controls drive.
- The radio strip and ACFT panel use roughly 200 px before any control is reached.
- Dangerous TX controls (Jammer, Transmit) sit far from the radio selector.

## Radio identity (shared by all options)
Each radio gets a fixed colour and a serial tag, for example `G40-0001-SIM`.
Colours: amber `#d0a644`, teal `#4fa3a5`, olive `#8aa24a`. TX/error red stays reserved for TX and conflicts.
The tag and colour are repeated in every panel header, so the radio is clear at any scroll position.

## Options (mockups: `A_..D_*.html`, screenshots `opt_*.png`)

| | A. Dense grid + sticky banner | B. Radio tabs + function tabs | C. Per-radio columns | D. Command strip + side rail |
|---|---|---|---|---|
| Scrolling | None at 1440x900 | None (content is switched) | None | None |
| Identity | Banner, tag on every panel | Colour-topped tab, tag in header and in every section | Whole column is one radio | Left colour band, tag on every panel, controlling-radio selector |
| Pros | Everything visible at once. Closest to current mental model, easy to port. | Scales to many radios. Large spectrum. | Impossible to confuse radios. Compare radios side by side. | Controls always in reach. Spectrum is the hero. Secondary panels collapse. |
| Cons | Dense. Only one radio at a time. | Hidden panels mean more clicks. TX is out of sight. | Cramped with 3+ radios. Narrow spectrum. Needs horizontal scroll over 3-4 radios. | Collapsed panels hide jammer/IQ state. Strip is busy. |
| Effort | Low-medium | Medium | Medium-high (parallel cards per radio) | Medium |

## Recommendation
**Option D**, with A's sticky "active radio" banner folded into its command strip.
It matches how an operator works (select radio, tune, watch spectrum, occasionally TX), removes scrolling, and puts radio identity in the strip plus a colour band and tag on every panel.
Use **Option C** as a "compare" mode toggle when 2-3 radios are linked, since it is the safest against radio mix-ups.
Suggested implementation: reuse the existing per-serial `cards` dict, add a `radio_tag(serial)` helper that renders the tag and colour, and assign colours by order of discovery.
