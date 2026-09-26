# Isolated rejection response experiment

Frozen before evaluation: quote_tape_v1 requires directional displacement >=1.5
bps of the executable entry quote AND >=1.5 bps of at least two executions strictly
after the first observed reclaim in the current episode. Only prints available at
the observation fence are used. Existing absorption timing remains; no extra hold
or multi-horizon unanimity is introduced. A new episode resets this research anchor.

Switch: research_rejection_response_policy=quote_tape_v1. Default legacy. Stop,
target, partial, leverage, fee schedule, risk and profile flags are unchanged.
This is a hypothesis, not a defect fix. Synthetic mirrored cases test disagreement,
old tape, future tape and a valid response. Historical causal reclaim quote was
not recorded as a distinct field in v2. Snapshot-only comparison cannot reproduce
this exact candidate without sequential state reconstruction; no historical wins
are claimed eliminated or preserved on that basis.

Payoff controls: existing R01 risk/broker payoff tests and six captured execution
controls retain target, original ETH stop, fees and profitable ZEC partial. Narrow
stops relative to full cycle costs and disabled uneconomic partials remain policy
questions. There is no demonstrated arithmetic discrepancy justifying widened
stops or forced partial. The immediate runner stop following positive partial is
preserved, including the winning control. Portfolio comparison remains unknown.
