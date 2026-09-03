# Hybrid application fixtures

These small local pages are deterministic smoke targets for the browser extension. They intentionally expose the same provider-style controls that the finisher must reconcile: file inputs, required facts, role writing, multipage navigation, validation failures, and a Quora-style Yes/No control that must not oscillate.

Run them from a local static server and load the unpacked `browser-extension/` during Playwright or Chrome smoke checks. They contain no real credentials, resume bytes, or employer data.
