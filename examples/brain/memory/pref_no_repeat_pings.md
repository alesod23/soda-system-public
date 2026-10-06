---
name: A card pings once
description: A pending card is never re-sent; a second post about the same subject updates the first card in place.
type: feedback
since: 2026-02-03
---
# A card pings once

Re-pinging a card that is still open is noise. Producers pass an `idem` key; the hub edits the existing message
silently instead of pushing a new one. A changed card says so in one line.
