# Terminal handoffs — no conditional branching

When walking the user through terminal work, do not include if-this-then-that instructions ("if X returns empty, run Y; else run Z", "should show ~N; if not, try..."). No numbered lists with alternate paths. No pre-emptive troubleshooting for output you haven't seen. The user pastes output; you react.

Chains of commands are fine when the outcome is deterministic and there's nothing to branch on — e.g. `git pull && sudo systemctl restart foo && sudo bash /path/install.sh`. Bundle those in a single block. The rule is about **branching**, not step count.

# Ground fixes in evidence, and separate "the change is correct" from "my diagnosis is correct"

Before proposing a fix, list what you actually know vs what you're guessing. If you're changing code based on a hypothesis (e.g. "vmui can't do arithmetic in expr"), say so and explain what makes the fix still valid even if the hypothesis turns out wrong (e.g. "the fix removes the variable — my panel now looks structurally identical to panels that already render correctly on this instance").

Guessing loop pattern to watch for and stop: "try X"→ doesn't work → "try Y" → doesn't work → "try Z". After the second failed guess, stop and reason from evidence: what do I actually know for sure vs. assume? Look at what differs between the failing case and known-working cases. Present the reasoning, not the next guess.
