# Terminal handoffs — no conditional branching

When walking the user through terminal work, do not include if-this-then-that instructions ("if X returns empty, run Y; else run Z", "should show ~N; if not, try..."). No numbered lists with alternate paths. No pre-emptive troubleshooting for output you haven't seen. The user pastes output; you react.

Chains of commands are fine when the outcome is deterministic and there's nothing to branch on — e.g. `git pull && sudo systemctl restart foo && sudo bash /path/install.sh`. Bundle those in a single block. The rule is about **branching**, not step count.
