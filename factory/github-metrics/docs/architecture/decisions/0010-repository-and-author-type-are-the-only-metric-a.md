# D10. Base metric attributes are repository and author type

- **Status:** accepted, 2026-11-14.
- **Rule:** the metric names and units follow appendix A. The review counter adds the review state, with four values. The cycle time histogram adds the end state and the start source, with two values each. The PR number and the author's login never become metric attributes. Milestones count when their end times are less than 24 hours old and fall after the store's creation time. The worker commits a milestone before it records the observation.
- **Undo:** rename or add instruments in a later release. Old series stop, and new ones start empty.
