// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package main

import "github.com/tbhb-dev/tbhbdev/factory/event-gateway/internal/filter"

// watch snapshots the subscriptions and cursor and registers onChange under
// the same lock so no change can slip between the snapshot and watching.
func (s *mcpSubscriptions) watch(onChange func()) ([]filter.Subscription, *uint64, func()) {
	s.mu.Lock()
	subs, cursor := append([]filter.Subscription{}, s.subs...), s.cursor
	s.onChange = onChange
	s.mu.Unlock()

	return subs, cursor, func() {
		s.mu.Lock()
		s.onChange = nil
		s.mu.Unlock()
	}
}
