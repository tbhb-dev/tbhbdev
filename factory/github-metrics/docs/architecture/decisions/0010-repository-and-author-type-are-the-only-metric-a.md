# D10. Repository and author type are the base metric attributes

- **Status:** accepted, 2026-11-14.
- **Rule:** the metric names and units follow appendix A. The review counter adds the review state, with four values. The cycle time histogram adds the end state and the start source, with two values each. The PR number and the author's login never become metric attributes, and a milestone counts if its age is under 24 hours and it occurred after the service created the store. The worker commits a milestone before it records the observation.
- **Undo:** rename or add instruments in a later release. Old series stop, and new ones start empty.
