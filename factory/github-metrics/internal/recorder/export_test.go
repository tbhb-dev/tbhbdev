// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package recorder

import "time"

// SetClock replaces the clock the eligibility rule reads.
func (r *Recorder) SetClock(now func() time.Time) { r.now = now }
